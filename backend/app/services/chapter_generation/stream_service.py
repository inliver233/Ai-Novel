from __future__ import annotations

import json
import logging
import time

from app.core.errors import AppError
from app.core.logging import exception_log_fields, log_event
from app.llm.client import call_llm_messages, call_llm_stream_messages
from app.schemas.chapter_generate import ChapterGenerateRequest
from app.services.chapter_generation.app_service import (
    apply_target_word_count,
    prepare_chapter_generate_request,
    run_plan_first_step,
)
from app.services.chapter_generation.models import PreparedChapterGenerateRequest
from app.services.generation_service import build_run_params_json
from app.services.generation_pipeline import run_content_optimize_step, run_post_edit_step
from app.services.llm_retry import (
    compute_backoff_seconds,
    is_retryable_llm_error,
    task_llm_max_attempts,
    task_llm_retry_base_seconds,
    task_llm_retry_jitter,
    task_llm_retry_max_seconds,
)
from app.services.output_contracts import contract_for_task
from app.services.run_store import write_generation_run
from app.utils.sse_response import (
    sse_chunk,
    sse_done,
    sse_error,
    sse_heartbeat,
    sse_progress,
    sse_result,
    sse_start,
    stream_blocking_call_with_heartbeat,
)

SSE_BLOCKING_STEP_HEARTBEAT_SECONDS = 1.0


def _chunk_text(text: str, *, chunk_size: int = 2048) -> list[str]:
    raw = str(text or "")
    if not raw:
        return []
    return [raw[i : i + chunk_size] for i in range(0, len(raw), chunk_size)]


def prepare_chapter_stream_request(
    *,
    logger: logging.Logger,
    request_id: str,
    chapter_id: str,
    body: ChapterGenerateRequest,
    user_id: str,
    x_llm_provider: str | None,
    x_llm_api_key: str | None,
) -> PreparedChapterGenerateRequest:
    prepared = prepare_chapter_generate_request(
        logger=logger,
        request_id=request_id,
        chapter_id=chapter_id,
        body=body,
        user_id=user_id,
        x_llm_provider=x_llm_provider,
        x_llm_api_key=x_llm_api_key,
        require_api_key=True,
    )
    if prepared.render_values is None:
        raise AppError(code="INTERNAL_ERROR", message="提示词变量准备失败", status_code=500)
    return prepared


def generate_chapter_stream_events(
    *,
    logger: logging.Logger,
    request_id: str,
    request_path: str,
    request_method: str,
    prepared: PreparedChapterGenerateRequest,
    chapter_id: str,
    body: ChapterGenerateRequest,
    user_id: str,
):
    plan_out: dict[str, object] | None = None
    plan_warnings: list[str] = []
    plan_parse_error: dict[str, object] | None = None
    raw_output = ""
    generation_run_id: str | None = None
    finish_reason: str | None = None
    dropped_params: list[str] = []
    latency_ms: int | None = None
    stream_run_written = False
    generation_started = False

    try:
        yield sse_start(message="开始生成...", progress=0)
        yield sse_progress(message="准备生成...", progress=0)

        if body.plan_first:
            plan_out, plan_warnings, plan_parse_error = yield from stream_blocking_call_with_heartbeat(
                runner=lambda: run_plan_first_step(
                    logger=logger,
                    prepared=prepared,
                    body=body,
                    actor_user_id=user_id,
                ),
                start_event=sse_progress(message="生成规划...", progress=5),
                heartbeat_event=sse_heartbeat(),
                heartbeat_interval_seconds=SSE_BLOCKING_STEP_HEARTBEAT_SECONDS,
            )
            if plan_parse_error is not None:
                err_code = str(plan_parse_error.get("code") or "PLAN_PARSE_ERROR")
                err_msg = str(plan_parse_error.get("message") or "无法解析规划输出")
                yield sse_progress(
                    message=f"规划解析失败（{err_code}）：{err_msg}（将继续生成） (request_id={request_id})",
                    progress=6,
                    status="error",
                )
            yield sse_progress(message="渲染章节提示词...", progress=8)

        apply_target_word_count(prepared=prepared, body=body)

        yield sse_progress(message="调用模型...", progress=10)
        generation_started = True

        target = body.target_word_count or 0
        max_attempts = task_llm_max_attempts(default=2)
        attempts: list[dict[str, object]] = []
        used_stream_fallback = False
        in_non_stream_fallback = False

        for attempt in range(1, max_attempts + 1):
            raw_output = ""
            last_progress = 10
            last_progress_ts = 0.0
            chunk_count = 0

            try:
                stream_iter, state = call_llm_stream_messages(
                    provider=prepared.llm_call.provider,
                    base_url=prepared.llm_call.base_url,
                    model=prepared.llm_call.model,
                    api_key=prepared.resolved_api_key,
                    messages=prepared.prompt_messages,
                    params=prepared.llm_call.params,
                    timeout_seconds=prepared.llm_call.timeout_seconds,
                    extra=prepared.llm_call.extra,
                )

                try:
                    for delta in stream_iter:
                        raw_output += delta
                        yield sse_chunk(delta)
                        chunk_count += 1
                        if chunk_count % 12 == 0:
                            yield sse_heartbeat()
                        now = time.monotonic()
                        if now - last_progress_ts >= 0.8:
                            if target > 0:
                                next_progress = 10 + int(min(1.0, len(raw_output) / float(target)) * 80)
                            else:
                                next_progress = 10 + int(min(1.0, len(raw_output) / 12000.0) * 80)
                            next_progress = max(last_progress, min(90, next_progress))
                            if next_progress != last_progress:
                                last_progress = next_progress
                                yield sse_progress(message="生成中...", progress=next_progress, char_count=len(raw_output))
                            last_progress_ts = now
                finally:
                    close = getattr(stream_iter, "close", None)
                    if callable(close):
                        close()

                finish_reason = state.finish_reason
                dropped_params = state.dropped_params
                latency_ms = state.latency_ms

                if chunk_count == 0 and not raw_output.strip():
                    used_stream_fallback = True
                    in_non_stream_fallback = True
                    yield sse_progress(message="未收到流式分片，回退非流式...", progress=12)

                    non_stream_attempts = task_llm_max_attempts(default=2)
                    for attempt2 in range(1, non_stream_attempts + 1):
                        try:
                            res2 = yield from stream_blocking_call_with_heartbeat(
                                runner=lambda: call_llm_messages(
                                    provider=prepared.llm_call.provider,
                                    base_url=prepared.llm_call.base_url,
                                    model=prepared.llm_call.model,
                                    api_key=prepared.resolved_api_key,
                                    messages=prepared.prompt_messages,
                                    params=prepared.llm_call.params,
                                    timeout_seconds=prepared.llm_call.timeout_seconds,
                                    extra=prepared.llm_call.extra,
                                ),
                                heartbeat_event=sse_heartbeat(),
                                heartbeat_interval_seconds=SSE_BLOCKING_STEP_HEARTBEAT_SECONDS,
                            )
                            raw_output = res2.text or ""
                            finish_reason = res2.finish_reason
                            dropped_params = res2.dropped_params
                            latency_ms = res2.latency_ms

                            parts = _chunk_text(raw_output)
                            for idx, part in enumerate(parts, start=1):
                                yield sse_chunk(part)
                                if idx % 12 == 0:
                                    yield sse_heartbeat()
                            break
                        except AppError as exc2:
                            retryable2 = is_retryable_llm_error(exc2)
                            attempts.append(
                                {
                                    "attempt": int(attempt2),
                                    "mode": "non_stream",
                                    "error_code": str(exc2.code),
                                    "status_code": int(exc2.status_code),
                                    "retryable": bool(retryable2),
                                }
                            )
                            if attempt2 >= non_stream_attempts or not retryable2:
                                if attempts:
                                    exc2.details = {
                                        **(exc2.details or {}),
                                        "attempts": attempts,
                                        "attempt_max": int(non_stream_attempts),
                                    }
                                raise

                            delay2 = compute_backoff_seconds(
                                attempt=attempt2 + 1,
                                base_seconds=task_llm_retry_base_seconds(),
                                max_seconds=task_llm_retry_max_seconds(),
                                jitter=task_llm_retry_jitter(),
                                error_code=str(exc2.code),
                            )
                            attempts[-1]["sleep_seconds"] = float(delay2)
                            yield sse_progress(message=f"非流式重试中（{attempt2 + 1}/{non_stream_attempts}）...", progress=12)
                            if delay2 > 0:
                                time.sleep(float(delay2))
                    in_non_stream_fallback = False
                    break

                break
            except AppError as exc:
                if in_non_stream_fallback:
                    if attempts:
                        exc.details = {
                            **(exc.details or {}),
                            "attempts": attempts,
                            "attempt_max": int(task_llm_max_attempts(default=2)),
                        }
                    raise

                retryable = is_retryable_llm_error(exc)
                attempts.append(
                    {
                        "attempt": int(attempt),
                        "mode": "stream",
                        "error_code": str(exc.code),
                        "status_code": int(exc.status_code),
                        "retryable": bool(retryable),
                    }
                )
                if chunk_count > 0 or attempt >= max_attempts or not retryable:
                    if attempts:
                        exc.details = {**(exc.details or {}), "attempts": attempts, "attempt_max": int(max_attempts)}
                    raise

                delay = compute_backoff_seconds(
                    attempt=attempt + 1,
                    base_seconds=task_llm_retry_base_seconds(),
                    max_seconds=task_llm_retry_max_seconds(),
                    jitter=task_llm_retry_jitter(),
                    error_code=str(exc.code),
                )
                attempts[-1]["sleep_seconds"] = float(delay)
                yield sse_progress(message=f"上游波动，重试中（{attempt + 1}/{max_attempts}）...", progress=10)
                if delay > 0:
                    time.sleep(float(delay))
                continue

        log_event(
            logger,
            "info",
            llm={
                "provider": prepared.llm_call.provider,
                "model": prepared.llm_call.model,
                "timeout_seconds": prepared.llm_call.timeout_seconds,
                "prompt_chars": len(prepared.prompt_system) + len(prepared.prompt_user),
                "output_chars": len(raw_output or ""),
                "dropped_params": dropped_params,
                "finish_reason": finish_reason,
                "stream": True,
            },
        )

        if used_stream_fallback or attempts:
            prepared.run_params_extra_json = prepared.run_params_extra_json or {}
            if used_stream_fallback:
                prepared.run_params_extra_json["stream_fallback"] = {"used": True}
            if attempts:
                prepared.run_params_extra_json["llm_retry"] = {"attempts": attempts}
            prepared.run_params_json = build_run_params_json(
                params_json=prepared.llm_call.params_json,
                memory_retrieval_log_json=None,
                extra_json=prepared.run_params_extra_json,
            )
        generation_run_id = write_generation_run(
            request_id=request_id,
            actor_user_id=user_id,
            project_id=prepared.project_id,
            chapter_id=chapter_id,
            run_type="chapter_stream",
            provider=prepared.llm_call.provider,
            model=prepared.llm_call.model,
            prompt_system=prepared.prompt_system,
            prompt_user=prepared.prompt_user,
            prompt_render_log_json=prepared.prompt_render_log_json,
            params_json=prepared.run_params_json or prepared.llm_call.params_json,
            output_text=raw_output,
            error_json=None,
        )
        stream_run_written = True

        yield sse_progress(message="解析输出...", progress=90)
        chapter_contract = contract_for_task("chapter_generate")
        parsed = chapter_contract.parse(raw_output, finish_reason=finish_reason)
        data, warnings, parse_error = parsed.data, parsed.warnings, parsed.parse_error

        if body.post_edit:
            raw_content = str(data.get("content_md") or "").strip()
            post_edit_applied = False
            post_edit_warnings: list[str] = []
            post_edit_parse_error: dict[str, object] | None = None

            if raw_content:
                data["post_edit_raw_content_md"] = raw_content
                step = yield from stream_blocking_call_with_heartbeat(
                    runner=lambda: run_post_edit_step(
                        logger=logger,
                        request_id=request_id,
                        actor_user_id=user_id,
                        project_id=prepared.project_id,
                        chapter_id=chapter_id,
                        api_key=prepared.resolved_api_key,
                        llm_call=prepared.llm_call,
                        render_values=prepared.render_values or {},
                        raw_content=raw_content,
                        macro_seed=f"{prepared.macro_seed}:post_edit",
                        post_edit_sanitize=bool(body.post_edit_sanitize),
                        run_params_extra_json={
                            **(prepared.run_params_extra_json or {}),
                            "post_edit_sanitize": bool(body.post_edit_sanitize),
                        },
                    ),
                    start_event=sse_progress(message="润色中...", progress=95),
                    heartbeat_event=sse_heartbeat(),
                    heartbeat_interval_seconds=SSE_BLOCKING_STEP_HEARTBEAT_SECONDS,
                )
                post_edit_warnings = step.warnings
                post_edit_parse_error = step.parse_error
                data["post_edit_run_id"] = step.run_id
                data["post_edit_edited_content_md"] = step.edited_content_md
                if step.applied:
                    data["content_md"] = step.edited_content_md
                    post_edit_applied = True
            else:
                post_edit_warnings.append("post_edit_no_content")

            data["post_edit_applied"] = post_edit_applied
            if post_edit_warnings:
                data["post_edit_warnings"] = post_edit_warnings
            if post_edit_parse_error is not None:
                data["post_edit_parse_error"] = post_edit_parse_error

        if body.content_optimize:
            raw_content = str(data.get("content_md") or "").strip()
            content_optimize_applied = False
            content_optimize_warnings: list[str] = []
            content_optimize_parse_error: dict[str, object] | None = None

            if raw_content:
                data["content_optimize_raw_content_md"] = raw_content
                step = yield from stream_blocking_call_with_heartbeat(
                    runner=lambda: run_content_optimize_step(
                        logger=logger,
                        request_id=request_id,
                        actor_user_id=user_id,
                        project_id=prepared.project_id,
                        chapter_id=chapter_id,
                        api_key=prepared.resolved_api_key,
                        llm_call=prepared.llm_call,
                        render_values=prepared.render_values or {},
                        raw_content=raw_content,
                        macro_seed=f"{prepared.macro_seed}:content_optimize",
                        run_params_extra_json={**(prepared.run_params_extra_json or {}), "content_optimize": True},
                    ),
                    start_event=sse_progress(message="正文优化中...", progress=97),
                    heartbeat_event=sse_heartbeat(),
                    heartbeat_interval_seconds=SSE_BLOCKING_STEP_HEARTBEAT_SECONDS,
                )
                content_optimize_warnings = step.warnings
                content_optimize_parse_error = step.parse_error
                data["content_optimize_run_id"] = step.run_id
                data["content_optimize_optimized_content_md"] = step.optimized_content_md
                if step.applied:
                    data["content_md"] = step.optimized_content_md
                    content_optimize_applied = True
            else:
                content_optimize_warnings.append("content_optimize_no_content")

            data["content_optimize_applied"] = content_optimize_applied
            if content_optimize_warnings:
                data["content_optimize_warnings"] = content_optimize_warnings
            if content_optimize_parse_error is not None:
                data["content_optimize_parse_error"] = content_optimize_parse_error

        if warnings:
            data["warnings"] = warnings
        if parse_error is not None:
            data["parse_error"] = parse_error
        if body.plan_first:
            data["plan"] = str((plan_out or {}).get("plan") or "")
            if plan_warnings:
                data["plan_warnings"] = plan_warnings
            if plan_parse_error is not None:
                data["plan_parse_error"] = plan_parse_error
        if finish_reason is not None:
            data["finish_reason"] = finish_reason
        if latency_ms is not None:
            data["latency_ms"] = latency_ms
        if dropped_params:
            data["dropped_params"] = dropped_params
        if generation_run_id is not None:
            data["generation_run_id"] = generation_run_id

        yield sse_progress(message="完成", progress=100, status="success")
        yield sse_result(data)
        yield sse_done()
    except GeneratorExit:
        # 客户端断开必须留结构化痕迹（backend-generation#9）；已启动的阻塞
        # LLM 步骤无法协作中断，由任务边界与 watchdog 收敛。
        log_event(
            logger,
            "warning",
            event="SSE_CLIENT_DISCONNECTED",
            request_id=request_id,
            chapter_id=chapter_id,
            generation_started=generation_started,
        )
        return
    except AppError as exc:
        if generation_started and not stream_run_written:
            write_generation_run(
                request_id=request_id,
                actor_user_id=user_id,
                project_id=prepared.project_id,
                chapter_id=chapter_id,
                run_type="chapter_stream",
                provider=prepared.llm_call.provider,
                model=prepared.llm_call.model,
                prompt_system=prepared.prompt_system,
                prompt_user=prepared.prompt_user,
                prompt_render_log_json=prepared.prompt_render_log_json,
                params_json=prepared.run_params_json or prepared.llm_call.params_json,
                output_text=raw_output or None,
                error_json=json.dumps({"code": exc.code, "message": exc.message, "details": exc.details}, ensure_ascii=False),
            )
        yield sse_error(error=f"{exc.message} ({exc.code})", code=exc.status_code)
        yield sse_done()
    except Exception as exc:
        log_event(
            logger,
            "error",
            error="SSE_STREAM_ERROR",
            path=request_path,
            method=request_method,
            chapter_id=chapter_id,
            **exception_log_fields(exc),
        )
        if generation_started and not stream_run_written:
            err_fields = dict(exception_log_fields(exc))
            err_fields.pop("stack", None)
            write_generation_run(
                request_id=request_id,
                actor_user_id=user_id,
                project_id=prepared.project_id,
                chapter_id=chapter_id,
                run_type="chapter_stream",
                provider=prepared.llm_call.provider,
                model=prepared.llm_call.model,
                prompt_system=prepared.prompt_system,
                prompt_user=prepared.prompt_user,
                prompt_render_log_json=prepared.prompt_render_log_json,
                params_json=prepared.run_params_json or prepared.llm_call.params_json,
                output_text=raw_output or None,
                error_json=json.dumps(
                    {
                        "code": "INTERNAL_ERROR",
                        "message": "服务器内部错误",
                        "details": err_fields,
                    },
                    ensure_ascii=False,
                ),
            )
        yield sse_error(error="服务器内部错误", code=500)
        yield sse_done()
