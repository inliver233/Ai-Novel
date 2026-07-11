from __future__ import annotations

import json
import logging

from sqlalchemy import select

from app.core.errors import AppError
from app.core.logging import log_event
from app.db.session import SessionLocal
from app.db.utils import utc_now
from app.models.batch_generation_task import BatchGenerationTask, BatchGenerationTaskItem
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_settings import ProjectSettings
from app.services.batch_generation_helpers import (
    BATCH_GENERATION_PROJECT_TASK_KIND,
    BatchGenerateParams,
    _iso,
    _json_dumps,
    _load_batch_project_task,
    _parse_params,
    append_batch_project_task_event,
    build_batch_generation_checkpoint,
    build_batch_step_payload,
    ensure_batch_generation_project_task,
    finalize_batch_project_task,
    mark_batch_project_task_running,
    pause_batch_generation,
    recalculate_batch_generation_counts,
    requeue_batch_project_task,
    sync_batch_generation_checkpoint,
    touch_batch_project_task,
)
from app.services.chapter_context_service import (
    PREVIOUS_CHAPTER_ENDING_CHARS,
    assemble_chapter_generate_render_values,
    build_smart_context,
    inject_plan_into_render_values,
    load_previous_chapter_context,
)
from app.services.generation_service import PreparedLlmCall, with_param_overrides
from app.services.generation_pipeline import run_chapter_generate_llm_step, run_content_optimize_step, run_plan_llm_step, run_post_edit_step
from app.services.length_control import estimate_max_tokens
from app.services.llm_task_preset_resolver import resolve_task_llm_config
from app.services.style_resolution_service import resolve_style_guide
from app.services.prompt_presets import ensure_default_plan_preset, render_preset_for_task
from app.services.prompt_store import format_characters

logger = logging.getLogger("ainovel")


def _cancel_task(task_id: str) -> None:
    with SessionLocal() as db:
        task = db.get(BatchGenerationTask, task_id)
        if task is None:
            return
        task.status = "canceled"
        task.pause_requested = False
        sync_batch_generation_checkpoint(task)
        items = (
            db.execute(
                select(BatchGenerationTaskItem).where(
                    BatchGenerationTaskItem.task_id == task_id, BatchGenerationTaskItem.status.in_(["queued", "running"])
                )
            )
            .scalars()
            .all()
        )
        for item in items:
            item.status = "canceled"
            item.finished_at = utc_now()
        finalize_batch_project_task(
            db,
            batch_task=task,
            status="canceled",
            event_type="canceled",
            result={"canceled": True, "batch_task_id": str(task.id)},
            payload={"reason": "batch_generation_cancel"},
        )
        db.commit()


def _prepare_project_context(
    *,
    project_id: str,
    outline_id: str,
    actor_user_id: str,
    params: BatchGenerateParams,
    expected_runtime_provider: str,
) -> tuple[Project, PreparedLlmCall, str, str, str, str, str, str, dict[str, object]]:
    with SessionLocal() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise AppError.not_found()

        resolved_task = resolve_task_llm_config(
            db,
            project=project,
            user_id=actor_user_id,
            task_key="chapter_generate",
            header_api_key=None,
        )
        if resolved_task is None:
            raise AppError(code="LLM_CONFIG_ERROR", message="请先在 Prompts 页保存 LLM 配置", status_code=400)

        llm_call = resolved_task.llm_call
        expected_provider = str(expected_runtime_provider or "").strip()
        if not expected_provider or llm_call.provider != expected_provider:
            raise AppError(
                code="BATCH_GENERATION_PROVIDER_DRIFT",
                message="批量生成任务的模型提供方配置已变化，请确认配置后重试",
                status_code=409,
                details={
                    "expected_provider": expected_provider or None,
                    "resolved_provider": llm_call.provider,
                },
            )
        resolved_api_key = resolved_task.api_key

        settings_row = db.get(ProjectSettings, project_id)
        outline_row = db.get(Outline, outline_id)

        world_setting = (settings_row.world_setting if settings_row else "") or ""
        style_guide = (settings_row.style_guide if settings_row else "") or ""
        constraints = (settings_row.constraints if settings_row else "") or ""

        if not params.include_world_setting:
            world_setting = ""
        if not params.include_style_guide:
            style_guide = ""
        if not params.include_constraints:
            constraints = ""

        outline_text = (outline_row.content_md if outline_row else "") or ""
        if not params.include_outline:
            outline_text = ""

        chars: list[Character] = []
        if params.character_ids:
            chars = (
                db.execute(
                    select(Character).where(
                        Character.project_id == project_id,
                        Character.id.in_(params.character_ids),
                    )
                )
                .scalars()
                .all()
            )
        characters_text = format_characters(chars)

        resolved_style_guide, style_resolution = resolve_style_guide(
            db,
            project_id=project_id,
            user_id=actor_user_id,
            requested_style_id=params.style_id,
            include_style_guide=bool(params.include_style_guide),
            settings_style_guide=style_guide,
        )

        return (
            project,
            llm_call,
            resolved_api_key,
            world_setting,
            resolved_style_guide,
            constraints,
            characters_text,
            outline_text,
            style_resolution,
        )


def run_batch_generation_task(*, task_id: str) -> None:
    """
    Background worker: generate chapters sequentially and persist results as generation_runs + task item status.

    IMPORTANT: Do not write generated content into `chapters` (demo contract: user must click Save).
    """
    with SessionLocal() as db:
        task = db.get(BatchGenerationTask, task_id)
        if task is None:
            return
        if task.status in ("succeeded", "failed", "canceled", "paused"):
            return
        if task.cancel_requested:
            task.status = "canceled"
            task.pause_requested = False
            sync_batch_generation_checkpoint(task)
            finalize_batch_project_task(
                db,
                batch_task=task,
                status="canceled",
                event_type="canceled",
                result={"canceled": True, "batch_task_id": str(task.id)},
                payload={"reason": "cancel_requested_before_start"},
            )
            db.commit()
            return
        if task.pause_requested:
            task.status = "paused"
            sync_batch_generation_checkpoint(task)
            finalize_batch_project_task(
                db,
                batch_task=task,
                status="paused",
                event_type="paused",
                result={"paused": True, "batch_task_id": str(task.id)},
                payload={"reason": "pause_requested_before_start"},
            )
            db.commit()
            return
        task.status = "running"
        sync_batch_generation_checkpoint(task)
        mark_batch_project_task_running(db, batch_task=task)
        db.commit()

        params = _parse_params(task)
        actor_user_id = task.actor_user_id or "local-user"

        rows = db.execute(
            select(BatchGenerationTaskItem.id, BatchGenerationTaskItem.chapter_id, BatchGenerationTaskItem.chapter_number, BatchGenerationTaskItem.status)
            .where(BatchGenerationTaskItem.task_id == task_id)
            .order_by(BatchGenerationTaskItem.chapter_number.asc())
        ).all()
        recalculate_batch_generation_counts(db, batch_task=task)
        db.commit()

    try:
        (
            project,
            llm_call_base,
            resolved_api_key,
            world_setting,
            style_guide,
            constraints,
            characters_text,
            outline_text,
            style_resolution,
        ) = _prepare_project_context(
            project_id=task.project_id,
            outline_id=task.outline_id,
            actor_user_id=actor_user_id,
            params=params,
            expected_runtime_provider=str(task.runtime_provider or ""),
        )
        run_params_extra_json = {"style_resolution": style_resolution}
    except AppError as exc:
        with SessionLocal() as db:
            task = db.get(BatchGenerationTask, task_id)
            if task is not None:
                pause_batch_generation(
                    db,
                    batch_task=task,
                    reason="prepare_project_context_failed",
                    source="batch_generation_worker",
                    error={"code": exc.code, "message": exc.message, "details": exc.details},
                )
                db.commit()
        return

    prev_content_md: str | None = None
    prev_summary: str | None = None

    for item_id, chapter_id, chapter_number, status in rows:
        if status == "succeeded":
            continue

        with SessionLocal() as db:
            task = db.get(BatchGenerationTask, task_id)
            if task is None:
                return
            if task.cancel_requested:
                db.commit()
                _cancel_task(task_id)
                return
            if task.pause_requested:
                pause_batch_generation(
                    db,
                    batch_task=task,
                    reason="pause_requested_before_step",
                    source="batch_generation_worker",
                    payload={"chapter_number": int(chapter_number)},
                )
                db.commit()
                return
            touch_batch_project_task(db, batch_task=task)

            item = db.get(BatchGenerationTaskItem, item_id)
            if item is None:
                task.status = "failed"
                task.failed_count = max(int(getattr(task, "failed_count", 0) or 0), 1)
                task.error_json = json.dumps({"code": "DB_ERROR", "message": "任务 item 不存在"}, ensure_ascii=False)
                sync_batch_generation_checkpoint(task)
                finalize_batch_project_task(
                    db,
                    batch_task=task,
                    status="failed",
                    event_type="failed",
                    error={"code": "DB_ERROR", "message": "任务 item 不存在"},
                    payload={"reason": "batch_item_missing", "chapter_number": int(chapter_number)},
                )
                db.commit()
                return
            if item.status in {"succeeded", "skipped", "failed"}:
                continue

            chapter_request_id = f"batch:{task_id}:{str(chapter_id or '')[:8]}"
            item.status = "running"
            item.attempt_count = int(getattr(item, "attempt_count", 0) or 0) + 1
            item.started_at = utc_now()
            item.finished_at = None
            item.last_request_id = chapter_request_id
            item.last_error_json = None
            item.error_message = None
            append_batch_project_task_event(
                db,
                batch_task=task,
                event_type="step_started",
                source="batch_generation_worker",
                payload={
                    "reason": "chapter_started",
                    "step": build_batch_step_payload(item),
                    "checkpoint": build_batch_generation_checkpoint(task),
                },
            )
            db.commit()

            chapter = db.get(Chapter, chapter_id) if chapter_id else None
            if chapter is None:
                item.status = "failed"
                item.finished_at = utc_now()
                item.last_request_id = chapter_request_id
                item.last_error_json = _json_dumps({"code": "NOT_FOUND", "message": "章节不存在"})
                item.error_message = "章节不存在"
                pause_batch_generation(
                    db,
                    batch_task=task,
                    reason="chapter_missing",
                    source="batch_generation_worker",
                    error={"code": "NOT_FOUND", "message": "章节不存在"},
                    item=item,
                    payload={"chapter_number": int(chapter_number)},
                )
                db.commit()
                return

            prev_text = ""
            prev_ending = ""
            mode = params.previous_chapter or "none"
            if prev_content_md is not None or prev_summary is not None:
                if mode == "summary":
                    prev_text = (prev_summary or "").strip()
                elif mode == "content":
                    prev_text = (prev_content_md or "").strip()
                elif mode == "tail":
                    raw_prev = (prev_content_md or "").strip()
                    prev_ending = raw_prev[-PREVIOUS_CHAPTER_ENDING_CHARS:].lstrip() if raw_prev else ""
            else:
                prev_text, prev_ending = load_previous_chapter_context(
                    db,
                    project_id=task.project_id,
                    outline_id=task.outline_id,
                    chapter_number=int(chapter.number),
                    previous_chapter=mode,
                )

            smart_recent_summaries = ""
            smart_recent_full = ""
            smart_story_skeleton = ""
            if params.include_smart_context:
                smart_recent_summaries, smart_recent_full, smart_story_skeleton = build_smart_context(
                    db,
                    project_id=task.project_id,
                    outline_id=task.outline_id,
                    chapter_number=int(chapter.number),
                )

        chapter_request_id = f"batch:{task_id}:{str(chapter_id or '')[:8]}"
        base_instruction = params.instruction
        instruction = f"【替换模式】输出完整替换稿（整章）。\n{base_instruction}".strip()

        values, requirements_obj = assemble_chapter_generate_render_values(
            project=project,
            mode="replace",
            chapter_number=int(chapter_number),
            chapter_title=(chapter.title or ""),
            chapter_plan=(chapter.plan or ""),
            world_setting=world_setting,
            style_guide=style_guide,
            constraints=constraints,
            characters_text=characters_text,
            entries_text="",
            outline_text=outline_text,
            instruction=instruction,
            target_word_count=params.target_word_count,
            previous_chapter=prev_text,
            previous_chapter_ending=prev_ending,
            current_draft_tail="",
            smart_context_recent_summaries=smart_recent_summaries,
            smart_context_recent_full=smart_recent_full,
            smart_context_story_skeleton=smart_story_skeleton,
        )

        try:
            llm_call = llm_call_base
            prompt_system = ""
            prompt_user = ""
            prompt_messages = []
            prompt_render_log_json: str | None = None
            render_values = values

            if params.plan_first:
                with SessionLocal() as db:
                    plan_llm_call = llm_call
                    plan_api_key = resolved_api_key
                    resolved_plan = resolve_task_llm_config(
                        db,
                        project=project,
                        user_id=actor_user_id,
                        task_key="plan_chapter",
                        header_api_key=None,
                    )
                    if resolved_plan is not None:
                        plan_llm_call = resolved_plan.llm_call
                        plan_api_key = resolved_plan.api_key
                    ensure_default_plan_preset(db, project_id=task.project_id)
                    plan_values = dict(values)
                    plan_values["instruction"] = base_instruction
                    plan_values["user"] = {"instruction": base_instruction, "requirements": requirements_obj}
                    plan_system, plan_user, plan_messages, _, _, _, plan_render_log = render_preset_for_task(
                        db,
                        project_id=task.project_id,
                        task="plan_chapter",
                        values=plan_values,  # type: ignore[arg-type]
                        macro_seed=f"{chapter_request_id}:plan",
                        provider=plan_llm_call.provider,
                    )
                plan_render_log_json = json.dumps(plan_render_log, ensure_ascii=False)
                plan_step = run_plan_llm_step(
                    logger=logger,
                    request_id=f"{chapter_request_id}:plan",
                    actor_user_id=actor_user_id,
                    project_id=task.project_id,
                    chapter_id=chapter_id,
                    api_key=str(plan_api_key),
                    llm_call=plan_llm_call,
                    prompt_system=plan_system,
                    prompt_user=plan_user,
                    prompt_messages=plan_messages,
                    prompt_render_log_json=plan_render_log_json,
                    run_params_extra_json=run_params_extra_json,
                )
                plan_text = str((plan_step.plan_out or {}).get("plan") or "").strip()
                if plan_text:
                    render_values = inject_plan_into_render_values(render_values, plan_text=plan_text)

            with SessionLocal() as db:
                prompt_system, prompt_user, prompt_messages, _, _, _, render_log = render_preset_for_task(
                    db,
                    project_id=task.project_id,
                    task="chapter_generate",
                    values=render_values,  # type: ignore[arg-type]
                    macro_seed=chapter_request_id,
                    provider=llm_call.provider,
                )
            prompt_render_log_json = json.dumps(render_log, ensure_ascii=False)

            if params.target_word_count is not None:
                llm_call = with_param_overrides(
                    llm_call,
                    {"max_tokens": estimate_max_tokens(target_word_count=params.target_word_count, provider=llm_call.provider, model=llm_call.model)},
                )

            gen_step = run_chapter_generate_llm_step(
                logger=logger,
                request_id=chapter_request_id,
                actor_user_id=actor_user_id,
                project_id=task.project_id,
                chapter_id=chapter_id,
                run_type="chapter",
                api_key=str(resolved_api_key),
                llm_call=llm_call,
                prompt_system=prompt_system,
                prompt_user=prompt_user,
                prompt_messages=prompt_messages,
                prompt_render_log_json=prompt_render_log_json,
                run_params_extra_json=run_params_extra_json,
            )
            data = gen_step.data
            rewrite_warnings: dict[str, list[str]] = {}

            if params.post_edit:
                raw_content = str(data.get("content_md") or "").strip()
                if raw_content:
                    step = run_post_edit_step(
                        logger=logger,
                        request_id=f"{chapter_request_id}:post_edit",
                        actor_user_id=actor_user_id,
                        project_id=task.project_id,
                        chapter_id=chapter_id,
                        api_key=str(resolved_api_key),
                        llm_call=llm_call,
                        render_values=render_values,
                        raw_content=raw_content,
                        macro_seed=f"{chapter_request_id}:post_edit",
                        post_edit_sanitize=bool(params.post_edit_sanitize),
                        run_params_extra_json={**run_params_extra_json, "post_edit_sanitize": bool(params.post_edit_sanitize)},
                    )
                    if step.warnings:
                        rewrite_warnings["post_edit"] = list(step.warnings)
                    if step.applied:
                        data["content_md"] = step.edited_content_md

            if params.content_optimize:
                raw_content = str(data.get("content_md") or "").strip()
                if raw_content:
                    step = run_content_optimize_step(
                        logger=logger,
                        request_id=f"{chapter_request_id}:content_optimize",
                        actor_user_id=actor_user_id,
                        project_id=task.project_id,
                        chapter_id=chapter_id,
                        api_key=str(resolved_api_key),
                        llm_call=llm_call,
                        render_values=render_values,
                        raw_content=raw_content,
                        macro_seed=f"{chapter_request_id}:content_optimize",
                        run_params_extra_json={**run_params_extra_json, "content_optimize": True},
                    )
                    if step.warnings:
                        rewrite_warnings["content_optimize"] = list(step.warnings)
                    if step.applied:
                        data["content_md"] = step.optimized_content_md

            final_content = str(data.get("content_md") or "").strip()
            final_summary = str(data.get("summary") or "").strip()
            prev_content_md = final_content
            prev_summary = final_summary

            with SessionLocal() as db:
                task = db.get(BatchGenerationTask, task_id)
                item = db.get(BatchGenerationTaskItem, item_id)
                if task is None or item is None:
                    return
                item.status = "succeeded"
                item.generation_run_id = gen_step.run_id
                item.error_message = None
                item.last_error_json = None
                item.last_request_id = chapter_request_id
                item.finished_at = utc_now()
                recalculate_batch_generation_counts(db, batch_task=task)
                success_payload: dict[str, object] = {
                    "reason": "chapter_succeeded",
                    "step": build_batch_step_payload(item),
                    "checkpoint": build_batch_generation_checkpoint(task),
                }
                if rewrite_warnings:
                    success_payload["rewrite_warnings"] = rewrite_warnings
                append_batch_project_task_event(
                    db,
                    batch_task=task,
                    event_type="step_succeeded",
                    source="batch_generation_worker",
                    payload=success_payload,
                )
                if task.pause_requested:
                    pause_batch_generation(
                        db,
                        batch_task=task,
                        reason="pause_requested_after_step",
                        source="batch_generation_worker",
                        item=item,
                        payload={"chapter_number": int(chapter_number)},
                    )
                    db.commit()
                    return
                db.commit()
        except AppError as exc:
            log_event(
                logger,
                "warning",
                batch_generation={
                    "task_id": task_id,
                    "chapter_id": chapter_id,
                    "chapter_number": chapter_number,
                    "error_code": exc.code,
                },
            )
            with SessionLocal() as db:
                task = db.get(BatchGenerationTask, task_id)
                item = db.get(BatchGenerationTaskItem, item_id)
                if item is not None:
                    item.status = "failed"
                    item.error_message = f"{exc.message} ({exc.code})"
                    item.last_error_json = _json_dumps({"code": exc.code, "message": exc.message, "details": exc.details})
                    item.last_request_id = chapter_request_id
                    item.finished_at = utc_now()
                if task is not None:
                    pause_batch_generation(
                        db,
                        batch_task=task,
                        reason="chapter_failed",
                        source="batch_generation_worker",
                        error={"code": exc.code, "message": exc.message, "details": exc.details},
                        item=item,
                        payload={"chapter_number": int(chapter_number)},
                    )
                db.commit()
            return
        except Exception as exc:
            log_event(
                logger,
                "error",
                batch_generation={
                    "task_id": task_id,
                    "chapter_id": chapter_id,
                    "chapter_number": chapter_number,
                    "exception_type": type(exc).__name__,
                },
            )
            with SessionLocal() as db:
                task = db.get(BatchGenerationTask, task_id)
                item = db.get(BatchGenerationTaskItem, item_id)
                if item is not None:
                    item.status = "failed"
                    item.error_message = "批量生成失败"
                    item.last_error_json = _json_dumps({"code": "INTERNAL_ERROR", "message": type(exc).__name__})
                    item.last_request_id = chapter_request_id
                    item.finished_at = utc_now()
                if task is not None:
                    pause_batch_generation(
                        db,
                        batch_task=task,
                        reason="chapter_exception",
                        source="batch_generation_worker",
                        error={"code": "INTERNAL_ERROR", "message": "批量生成失败"},
                        item=item,
                        payload={"chapter_number": int(chapter_number)},
                    )
                db.commit()
            return

    with SessionLocal() as db:
        task = db.get(BatchGenerationTask, task_id)
        if task is None:
            return
        if task.cancel_requested:
            task.status = "canceled"
            task.pause_requested = False
            sync_batch_generation_checkpoint(task)
            finalize_batch_project_task(
                db,
                batch_task=task,
                status="canceled",
                event_type="canceled",
                result={"canceled": True, "batch_task_id": str(task.id)},
                payload={"reason": "cancel_requested_after_loop"},
            )
        elif task.status == "paused" or task.pause_requested:
            if task.status != "paused":
                pause_batch_generation(
                    db,
                    batch_task=task,
                    reason="pause_requested_after_loop",
                    source="batch_generation_worker",
                )
        elif task.status != "failed":
            task.pause_requested = False
            recalculate_batch_generation_counts(db, batch_task=task)
            task.status = "succeeded"
            sync_batch_generation_checkpoint(task)
            finalize_batch_project_task(
                db,
                batch_task=task,
                status="succeeded",
                event_type="succeeded",
                result={
                    "batch_task_id": str(task.id),
                    "total_count": int(task.total_count or 0),
                    "completed_count": int(task.completed_count or 0),
                    "failed_count": int(getattr(task, "failed_count", 0) or 0),
                    "skipped_count": int(getattr(task, "skipped_count", 0) or 0),
                },
                payload={"reason": "batch_generation_done"},
            )
        db.commit()
