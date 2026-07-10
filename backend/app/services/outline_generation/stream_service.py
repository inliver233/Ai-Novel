from __future__ import annotations

import concurrent.futures
import json
import logging
import threading
import time

from app.core.errors import AppError
from app.core.logging import log_event
from app.llm.client import call_llm_stream_messages
from app.schemas.outline_generate import OutlineGenerateRequest
from app.services.generation_service import call_llm_and_record, with_param_overrides
from app.services.outline_generation.chapter_ops import (
    _clone_outline_chapters,
    _dedupe_warnings,
    _enforce_outline_chapter_coverage,
    _normalize_outline_chapters,
)
from app.services.outline_generation.fill_service import _fill_outline_missing_chapters_with_llm
from app.services.outline_generation.models import PreparedOutlineGeneration
from app.services.outline_generation.policy import (
    OUTLINE_FILL_HEARTBEAT_INTERVAL_SECONDS,
    OUTLINE_FILL_POLL_INTERVAL_SECONDS,
    _outline_fill_progress_message,
    _outline_segment_progress_message,
    _should_use_outline_segmented_mode,
)
from app.services.outline_generation.prepare_service import _write_outline_segmented_aggregate_run
from app.services.outline_generation.stream_finalize_service import (
    finalize_outline_stream_result,
    finalize_segmented_outline_stream_result,
    write_outline_stream_error_run,
)
from app.services.outline_generation.stream_progress_service import (
    iter_fill_progress_sse_events,
    iter_segment_progress_sse_events,
)
from app.services.outline_generation.segment_service import _generate_outline_segmented_with_llm
from app.services.output_contracts import build_repair_prompt_for_task, contract_for_task
from app.services.run_store import write_generation_run
from app.utils.sse_response import (
    sse_chunk,
    sse_done,
    sse_error,
    sse_heartbeat,
    sse_progress,
    sse_result,
)

logger = logging.getLogger("ainovel")


def generate_outline_stream_events(
    *,
    request_id: str,
    project_id: str,
    body: OutlineGenerateRequest,
    user_id: str,
    prepared: PreparedOutlineGeneration,
):

    yield sse_progress(message="准备生成...", progress=0)

    prompt_system = prepared.prompt_system
    prompt_user = prepared.prompt_user
    prompt_messages = prepared.prompt_messages
    prompt_render_log_json = prepared.prompt_render_log_json
    llm_call = prepared.llm_call
    resolved_api_key = prepared.resolved_api_key
    target_chapter_count = prepared.target_chapter_count
    run_params_extra_json = prepared.run_params_extra_json
    run_params_json = prepared.run_params_json

    if _should_use_outline_segmented_mode(target_chapter_count):
        if target_chapter_count is None:
            yield sse_error(error="长篇分段模式参数异常", code=500)
            yield sse_done()
            return
        yield sse_progress(message="长篇模式：分段生成中...", progress=10)
        segment_progress_lock = threading.Lock()
        segment_progress_events: list[dict[str, object]] = []

        def _on_segment_progress(update: dict[str, object]) -> None:
            if not isinstance(update, dict):
                return
            with segment_progress_lock:
                segment_progress_events.append(dict(update))

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                _generate_outline_segmented_with_llm,
                request_id=request_id,
                actor_user_id=user_id,
                project_id=project_id,
                api_key=str(resolved_api_key),
                llm_call=llm_call,
                prompt_system=prompt_system,
                prompt_user=prompt_user,
                target_chapter_count=target_chapter_count,
                run_params_extra_json=run_params_extra_json,
                progress_hook=_on_segment_progress,
            )
            yield from iter_segment_progress_sse_events(
                future=future,
                progress_events=segment_progress_events,
                progress_lock=segment_progress_lock,
                heartbeat_interval=OUTLINE_FILL_HEARTBEAT_INTERVAL_SECONDS,
                poll_interval=OUTLINE_FILL_POLL_INTERVAL_SECONDS,
                progress_message_builder=_outline_segment_progress_message,
            )

            segmented = future.result()

        aggregate_run_id = _write_outline_segmented_aggregate_run(
            request_id=request_id,
            actor_user_id=user_id,
            project_id=project_id,
            run_type="outline_stream_segmented",
            llm_call=llm_call,
            prompt_system=prompt_system,
            prompt_user=prompt_user,
            prompt_render_log_json=prompt_render_log_json,
            run_params_json=run_params_json,
            data=segmented.data,
            warnings=segmented.warnings,
            parse_error=segmented.parse_error,
            segmented_run_ids=segmented.run_ids,
            meta=segmented.meta,
        )
        result_data = finalize_segmented_outline_stream_result(
            segmented=segmented,
            aggregate_run_id=aggregate_run_id,
            dedupe_warnings=_dedupe_warnings,
        )

        yield sse_progress(message="完成", progress=100, status="success")
        yield sse_result(result_data)
        yield sse_done()
        return

    yield sse_progress(message="调用模型...", progress=10)

    raw_output = ""
    generation_run_id: str | None = None
    finish_reason: str | None = None
    dropped_params: list[str] = []
    latency_ms: int | None = None
    stream_run_written = False

    try:
        stream_iter, state = call_llm_stream_messages(
            provider=llm_call.provider,
            base_url=llm_call.base_url,
            model=llm_call.model,
            api_key=str(resolved_api_key),
            messages=prompt_messages,
            params=llm_call.params,
            timeout_seconds=llm_call.timeout_seconds,
            extra=llm_call.extra,
        )

        last_progress = 10
        last_progress_ts = 0.0
        chunk_count = 0
        try:
            for delta in stream_iter:
                raw_output += delta
                yield sse_chunk(delta)
                chunk_count += 1
                if chunk_count % 12 == 0:
                    yield sse_heartbeat()
                now = time.monotonic()
                if now - last_progress_ts >= 0.8:
                    next_progress = 10 + int(min(1.0, len(raw_output) / 6000.0) * 80)
                    next_progress = max(last_progress, min(90, next_progress))
                    if next_progress != last_progress:
                        last_progress = next_progress
                        yield sse_progress(message="生成中...", progress=next_progress)
                    last_progress_ts = now
        finally:
            close = getattr(stream_iter, "close", None)
            if callable(close):
                close()

        finish_reason = state.finish_reason
        dropped_params = state.dropped_params
        latency_ms = state.latency_ms

        log_event(
            logger,
            "info",
            llm={
                "provider": llm_call.provider,
                "model": llm_call.model,
                "timeout_seconds": llm_call.timeout_seconds,
                "prompt_chars": len(prompt_system) + len(prompt_user),
                "output_chars": len(raw_output or ""),
                "dropped_params": dropped_params,
                "finish_reason": finish_reason,
                "stream": True,
            },
        )
        generation_run_id = write_generation_run(
            request_id=request_id,
            actor_user_id=user_id,
            project_id=project_id,
            chapter_id=None,
            run_type="outline_stream",
            provider=llm_call.provider,
            model=llm_call.model,
            prompt_system=prompt_system,
            prompt_user=prompt_user,
            prompt_render_log_json=prompt_render_log_json,
            params_json=run_params_json,
            output_text=raw_output,
            error_json=None,
        )
        stream_run_written = True

        yield sse_progress(message="解析输出...", progress=90)
        contract = contract_for_task("outline_generate")
        parsed = contract.parse(raw_output, finish_reason=finish_reason)
        data, warnings, parse_error = parsed.data, parsed.warnings, parsed.parse_error

        if parse_error is not None and llm_call.provider in (
            "openai",
            "openai_responses",
            "openai_compatible",
            "openai_responses_compatible",
            "anthropic",
            "gemini",
        ):
            yield sse_progress(message="尝试修复 JSON...", progress=92)
            repair = build_repair_prompt_for_task("outline_generate", raw_output=raw_output)
            if repair is None:
                warnings.append("outline_fix_json_failed")
                repair = None
            if repair is None:
                raise AppError(code="OUTLINE_FIX_UNSUPPORTED", message="该任务不支持输出修复", status_code=400)
            fix_system, fix_user, fix_run_type = repair
            # Use a generous max_tokens for repair — 1024 is far too small for multi-chapter outlines.
            # Use the original max_tokens (clamped to a reasonable floor) so the repair can output the full JSON.
            repair_max_tokens = max(int(llm_call.params.get("max_tokens") or 16384), 8192)
            fix_call = with_param_overrides(llm_call, {"temperature": 0, "max_tokens": repair_max_tokens})
            try:
                fixed = call_llm_and_record(
                    logger=logger,
                    request_id=request_id,
                    actor_user_id=user_id,
                    project_id=project_id,
                    chapter_id=None,
                    run_type=fix_run_type,
                    api_key=str(resolved_api_key),
                    prompt_system=fix_system,
                    prompt_user=fix_user,
                    llm_call=fix_call,
                    run_params_extra_json=run_params_extra_json,
                )
                fixed_parsed = contract.parse(fixed.text)
                fixed_data, fixed_warnings, fixed_error = (
                    fixed_parsed.data,
                    fixed_parsed.warnings,
                    fixed_parsed.parse_error,
                )
                if fixed_error is None and fixed_data.get("chapters"):
                    fixed_data["raw_output"] = raw_output
                    fixed_data["fixed_json"] = fixed_data.get("raw_json") or fixed.text
                    data = fixed_data
                    warnings.extend(["json_fixed_via_llm", *fixed_warnings])
                    parse_error = None
            except AppError:
                warnings.append("outline_fix_json_failed")

        if parse_error is None:
            data, coverage_warnings = _enforce_outline_chapter_coverage(
                data=data,
                target_chapter_count=target_chapter_count,
            )
            warnings.extend(coverage_warnings)
            preview_outline_md = str(data.get("outline_md") or "")
            preview_chapters, _preview_warnings = _normalize_outline_chapters(data.get("chapters"))
            if preview_chapters:
                yield sse_result(
                    {
                        "outline_md": preview_outline_md,
                        "chapters": _clone_outline_chapters(preview_chapters),
                    }
                )
            if target_chapter_count:
                yield sse_progress(message="补全缺失章节...", progress=94)
            fill_progress_lock = threading.Lock()
            fill_progress_events: list[dict[str, object]] = []

            def _on_fill_progress(update: dict[str, object]) -> None:
                if not isinstance(update, dict):
                    return
                with fill_progress_lock:
                    fill_progress_events.append(dict(update))

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                fill_future = executor.submit(
                    _fill_outline_missing_chapters_with_llm,
                    data=data,
                    target_chapter_count=target_chapter_count,
                    request_id=request_id,
                    actor_user_id=user_id,
                    project_id=project_id,
                    api_key=str(resolved_api_key),
                    llm_call=llm_call,
                    run_params_extra_json=run_params_extra_json,
                    progress_hook=_on_fill_progress,
                )
                yield from iter_fill_progress_sse_events(
                    future=fill_future,
                    progress_events=fill_progress_events,
                    progress_lock=fill_progress_lock,
                    heartbeat_interval=OUTLINE_FILL_HEARTBEAT_INTERVAL_SECONDS,
                    poll_interval=OUTLINE_FILL_POLL_INTERVAL_SECONDS,
                    progress_message_builder=_outline_fill_progress_message,
                    preview_outline_md=preview_outline_md,
                )

                data, fill_warnings, fill_run_ids = fill_future.result()
            warnings.extend(fill_warnings)
            if fill_run_ids:
                coverage = data.get("chapter_coverage")
                if isinstance(coverage, dict):
                    coverage["fill_run_ids"] = fill_run_ids
                    data["chapter_coverage"] = coverage

        result_data = finalize_outline_stream_result(
            data=data,
            warnings=warnings,
            parse_error=parse_error,
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            dropped_params=dropped_params,
            generation_run_id=generation_run_id,
            dedupe_warnings=_dedupe_warnings,
        )

        yield sse_progress(message="完成", progress=100, status="success")
        yield sse_result(result_data)
        yield sse_done()
    except GeneratorExit:
        return
    except AppError as exc:
        write_outline_stream_error_run(
            stream_run_written=stream_run_written,
            request_id=request_id,
            actor_user_id=user_id,
            project_id=project_id,
            llm_call=llm_call,
            prompt_system=prompt_system,
            prompt_user=prompt_user,
            prompt_render_log_json=prompt_render_log_json,
            run_params_json=run_params_json,
            output_text=raw_output or None,
            error_payload={"code": exc.code, "message": exc.message, "details": exc.details},
        )
        yield sse_error(error=f"{exc.message} ({exc.code})", code=exc.status_code)
        yield sse_done()
    except Exception:
        write_outline_stream_error_run(
            stream_run_written=stream_run_written,
            request_id=request_id,
            actor_user_id=user_id,
            project_id=project_id,
            llm_call=llm_call,
            prompt_system=prompt_system,
            prompt_user=prompt_user,
            prompt_render_log_json=prompt_render_log_json,
            run_params_json=run_params_json,
            output_text=raw_output or None,
            error_payload={"code": "INTERNAL_ERROR", "message": "服务器内部错误"},
        )
        yield sse_error(error="服务器内部错误", code=500)
        yield sse_done()
