from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Header, Request
from sqlalchemy import select

from app.api.deps import DbDep, UserIdDep, require_chapter_editor, require_project_editor, require_project_viewer
from app.core.config import settings
from app.core.errors import AppError, ok_payload
from app.db.utils import new_id, utc_now
from app.models.batch_generation_task import BatchGenerationTask, BatchGenerationTaskItem
from app.models.chapter import Chapter
from app.models.project_task import ProjectTask
from app.schemas.batch_generation import BatchGenerationCreateRequest, BatchGenerationTaskItemOut, BatchGenerationTaskOut
from app.services.batch_generation_service import (
    append_batch_project_task_event,
    build_batch_step_payload,
    build_batch_generation_checkpoint,
    ensure_batch_generation_project_task,
    finalize_batch_project_task,
    pause_batch_generation,
    recalculate_batch_generation_counts,
    requeue_batch_project_task,
    sync_batch_generation_checkpoint,
)
from app.services.batch_generation_quota import (
    enter_batch_generation_quota_admission,
    lock_and_enforce_batch_generation_quotas,
)
from app.services.llm_task_preset_resolver import resolve_task_runtime_provider
from app.services.outline_store import ensure_active_outline
from app.services.project_task_event_service import append_project_task_event
from app.services.task_queue import get_task_queue

router = APIRouter()


def _load_batch_items(db: DbDep, *, task_id: str) -> list[BatchGenerationTaskItem]:
    return (
        db.execute(select(BatchGenerationTaskItem).where(BatchGenerationTaskItem.task_id == task_id).order_by(BatchGenerationTaskItem.chapter_number.asc()))
        .scalars()
        .all()
    )


def _build_batch_task_response(db: DbDep, *, task_id: str) -> dict:
    db.expire_all()
    task = db.get(BatchGenerationTask, task_id)
    if task is None:
        raise AppError.not_found()
    items = _load_batch_items(db, task_id=task_id)
    out_task = BatchGenerationTaskOut.model_validate(task).model_dump()
    out_items = [BatchGenerationTaskItemOut.model_validate(i).model_dump() for i in items]
    return {"task": out_task, "items": out_items}


def _enqueue_batch_task_or_restore_paused(
    db: DbDep,
    *,
    task: BatchGenerationTask,
    source: str,
    reason: str,
) -> None:
    try:
        get_task_queue().enqueue_batch_generation_task(str(task.id))
    except AppError as exc:
        pause_batch_generation(
            db,
            batch_task=task,
            reason=reason,
            source=source,
            error={"code": exc.code, "message": exc.message, "details": exc.details},
        )
        db.commit()
        raise
    except Exception as exc:
        pause_batch_generation(
            db,
            batch_task=task,
            reason=reason,
            source=source,
            error={"code": "QUEUE_ENQUEUE_ERROR", "message": "批量生成任务入队失败", "details": {"error_type": type(exc).__name__}},
        )
        db.commit()
        raise AppError(code="QUEUE_ENQUEUE_ERROR", message="批量生成任务入队失败", status_code=503)


@router.post("/projects/{project_id}/batch_generation_tasks")
def create_batch_generation_task(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    body: BatchGenerationCreateRequest,
    _x_llm_provider: Annotated[str | None, Header(alias="X-LLM-Provider", max_length=64)] = None,
) -> dict:
    request_id = request.state.request_id
    project = require_project_editor(db, project_id=project_id, user_id=user_id)
    provider = resolve_task_runtime_provider(db, project_id=project_id, task_key="chapter_generate")

    if int(body.count) > int(settings.batch_generation_max_count or 200):
        raise AppError.validation(
            message=f"批量生成数量不能超过 {int(settings.batch_generation_max_count or 200)}",
            details={"max_count": int(settings.batch_generation_max_count or 200)},
        )

    if body.after_chapter_id:
        after = require_chapter_editor(db, chapter_id=body.after_chapter_id, user_id=user_id)
        if after.project_id != project_id:
            raise AppError.validation(message="起始章节（after_chapter_id）不属于当前项目")
        outline_id = after.outline_id
        start_number = int(after.number) + 1
    else:
        outline_id = ensure_active_outline(db, project=project).id
        start_number = 1

    limit = max(int(body.count) * 5, int(body.count))
    candidates = (
        db.execute(
            select(Chapter)
            .where(
                Chapter.project_id == project_id,
                Chapter.outline_id == outline_id,
                Chapter.number >= start_number,
            )
            .order_by(Chapter.number.asc())
            .limit(limit)
        )
        .scalars()
        .all()
    )

    def _is_empty(ch: Chapter) -> bool:
        return not ((ch.content_md or "").strip() or (ch.summary or "").strip())

    selected: list[Chapter] = []
    for ch in candidates:
        if not body.include_existing and not _is_empty(ch):
            continue
        selected.append(ch)
        if len(selected) >= int(body.count):
            break

    if not selected:
        raise AppError.validation(message="没有可生成的章节（请先创建章节，或开启 include_existing）")
    if len(selected) < int(body.count):
        raise AppError.validation(
            message=f"目标章节不足：仅找到 {len(selected)} 章可生成，请减少数量或开启 include_existing",
            details={"found": len(selected), "required": int(body.count)},
        )

    if body.context.require_sequential:
        selected_numbers = {int(ch.number) for ch in selected}
        max_num = max(selected_numbers)
        if max_num > 1:
            prev_rows = db.execute(
                select(Chapter.number, Chapter.content_md, Chapter.summary).where(
                    Chapter.project_id == project_id,
                    Chapter.outline_id == outline_id,
                    Chapter.number < max_num,
                )
            ).all()
            existing = {int(r[0]): (r[1], r[2]) for r in prev_rows}
            missing_numbers: list[int] = []
            for n in range(1, max_num):
                if n in selected_numbers:
                    continue
                content_md, summary = existing.get(n, (None, None))
                if not ((content_md or "").strip() or (summary or "").strip()):
                    missing_numbers.append(n)
            if missing_numbers:
                raise AppError(
                    code="CHAPTER_PREREQ_MISSING",
                    message=f"缺少前置章节内容：第 {', '.join(str(n) for n in missing_numbers)} 章",
                    status_code=400,
                    details={"missing_numbers": missing_numbers},
                )

    selected_refs = [(str(ch.id), int(ch.number)) for ch in selected]
    enter_batch_generation_quota_admission(db)
    require_project_editor(db, project_id=project_id, user_id=user_id)
    provider = resolve_task_runtime_provider(db, project_id=project_id, task_key="chapter_generate")
    selected_ids = [chapter_id for chapter_id, _number in selected_refs]
    selected = (
        db.execute(
            select(Chapter)
            .where(
                Chapter.id.in_(selected_ids),
                Chapter.project_id == project_id,
                Chapter.outline_id == outline_id,
            )
            .order_by(Chapter.number.asc())
            .with_for_update()
        )
        .scalars()
        .all()
    )
    if [(str(ch.id), int(ch.number)) for ch in selected] != selected_refs:
        raise AppError.conflict(
            message="批量生成目标章节在创建过程中发生变化，请重试",
            details={"reason": "chapter_selection_changed"},
        )
    if not body.include_existing and any(not _is_empty(ch) for ch in selected):
        raise AppError.conflict(
            message="批量生成目标章节已有新内容，请重试",
            details={"reason": "chapter_content_changed"},
        )

    lock_and_enforce_batch_generation_quotas(
        db,
        project_id=project_id,
        user_id=user_id,
        provider=provider,
    )

    task_id = new_id()
    task = BatchGenerationTask(
        id=task_id,
        project_id=project_id,
        outline_id=outline_id,
        actor_user_id=user_id,
        runtime_provider=provider,
        status="queued",
        total_count=len(selected),
        completed_count=0,
        failed_count=0,
        skipped_count=0,
        cancel_requested=False,
        pause_requested=False,
        params_json=json.dumps(body.model_dump(), ensure_ascii=False),
        checkpoint_json=None,
        error_json=None,
    )
    items = [
        BatchGenerationTaskItem(
            id=new_id(),
            task_id=task_id,
            chapter_id=ch.id,
            chapter_number=int(ch.number),
            status="queued",
            generation_run_id=None,
            error_message=None,
        )
        for ch in selected
    ]

    db.add(task)
    db.add_all(items)
    ensure_batch_generation_project_task(
        db,
        batch_task=task,
        chapter_numbers=[int(ch.number) for ch in selected],
        request_id=request_id,
    )
    db.commit()

    try:
        get_task_queue().enqueue_batch_generation_task(task_id)
    except AppError as exc:
        task.status = "failed"
        task.failed_count = max(int(task.failed_count or 0), 1)
        task.error_json = json.dumps({"code": exc.code, "message": exc.message, "details": exc.details}, ensure_ascii=False)
        sync_batch_generation_checkpoint(task)
        for item in items:
            if item.status == "queued":
                item.status = "failed"
                item.error_message = f"{exc.message} ({exc.code})"
        runtime_task = db.get(ProjectTask, task.project_task_id) if task.project_task_id else None
        if runtime_task is not None:
            runtime_task.status = "failed"
            runtime_task.error_json = json.dumps({"code": exc.code, "message": exc.message, "details": exc.details}, ensure_ascii=False)
            append_project_task_event(
                db,
                task=runtime_task,
                event_type="failed",
                source="batch_generation_enqueue",
                payload={
                    "reason": "enqueue_failed",
                    "checkpoint": build_batch_generation_checkpoint(task),
                    "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                },
            )
        db.commit()
        raise

    # In inline mode, the worker runs synchronously (separate session) and updates task/items.
    # Ensure we return fresh statuses/generation_run_id for the UI to apply results.
    db.expire_all()
    task = db.get(BatchGenerationTask, task_id) or task
    items = (
        db.execute(
            select(BatchGenerationTaskItem)
            .where(BatchGenerationTaskItem.task_id == task_id)
            .order_by(BatchGenerationTaskItem.chapter_number.asc())
        )
        .scalars()
        .all()
    )

    out_task = BatchGenerationTaskOut.model_validate(task).model_dump()
    out_items = [BatchGenerationTaskItemOut.model_validate(i).model_dump() for i in items]
    return ok_payload(request_id=request_id, data={"task": out_task, "items": out_items})


@router.get("/projects/{project_id}/batch_generation_tasks/active")
def get_active_batch_generation_task(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
) -> dict:
    request_id = request.state.request_id
    require_project_viewer(db, project_id=project_id, user_id=user_id)

    task = (
        db.execute(
            select(BatchGenerationTask)
            .where(
                BatchGenerationTask.project_id == project_id,
            )
            .order_by(BatchGenerationTask.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if task is None:
        return ok_payload(request_id=request_id, data={"task": None, "items": []})

    items = (
        db.execute(select(BatchGenerationTaskItem).where(BatchGenerationTaskItem.task_id == task.id).order_by(BatchGenerationTaskItem.chapter_number.asc()))
        .scalars()
        .all()
    )
    out_task = BatchGenerationTaskOut.model_validate(task).model_dump()
    out_items = [BatchGenerationTaskItemOut.model_validate(i).model_dump() for i in items]
    return ok_payload(request_id=request_id, data={"task": out_task, "items": out_items})


@router.get("/batch_generation_tasks/{task_id}")
def get_batch_generation_task(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    task_id: str,
) -> dict:
    request_id = request.state.request_id
    task = db.get(BatchGenerationTask, task_id)
    if task is None:
        raise AppError.not_found()
    require_project_viewer(db, project_id=task.project_id, user_id=user_id)
    items = (
        db.execute(select(BatchGenerationTaskItem).where(BatchGenerationTaskItem.task_id == task_id).order_by(BatchGenerationTaskItem.chapter_number.asc()))
        .scalars()
        .all()
    )
    out_task = BatchGenerationTaskOut.model_validate(task).model_dump()
    out_items = [BatchGenerationTaskItemOut.model_validate(i).model_dump() for i in items]
    return ok_payload(request_id=request_id, data={"task": out_task, "items": out_items})


@router.post("/batch_generation_tasks/{task_id}/pause")
def pause_batch_generation_task(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    task_id: str,
) -> dict:
    request_id = request.state.request_id
    task = db.get(BatchGenerationTask, task_id)
    if task is None:
        raise AppError.not_found()
    require_project_editor(db, project_id=task.project_id, user_id=user_id)

    if task.status not in ("queued", "running"):
        return ok_payload(request_id=request_id, data={"task": BatchGenerationTaskOut.model_validate(task).model_dump(), "paused": False})

    if task.pause_requested and task.status != "queued":
        return ok_payload(request_id=request_id, data={"task": BatchGenerationTaskOut.model_validate(task).model_dump(), "paused": False})

    task.pause_requested = True
    task.cancel_requested = False
    sync_batch_generation_checkpoint(task)
    if task.status == "queued":
        pause_batch_generation(
            db,
            batch_task=task,
            reason="manual_pause",
            source="batch_generation_pause",
        )
    else:
        append_batch_project_task_event(
            db,
            batch_task=task,
            event_type="checkpoint",
            source="batch_generation_pause",
            payload={
                "reason": "manual_pause_requested",
                "checkpoint": build_batch_generation_checkpoint(task),
            },
        )

    db.commit()
    return ok_payload(request_id=request_id, data={"task": BatchGenerationTaskOut.model_validate(task).model_dump(), "paused": True})


@router.post("/batch_generation_tasks/{task_id}/resume")
def resume_batch_generation_task(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    task_id: str,
) -> dict:
    request_id = request.state.request_id
    task = db.get(BatchGenerationTask, task_id)
    if task is None:
        raise AppError.not_found()
    require_project_editor(db, project_id=task.project_id, user_id=user_id)

    if task.status != "paused":
        return ok_payload(request_id=request_id, data={"task": BatchGenerationTaskOut.model_validate(task).model_dump(), "resumed": False})

    enter_batch_generation_quota_admission(db)
    task = db.get(BatchGenerationTask, task_id)
    if task is None:
        raise AppError.not_found()
    require_project_editor(db, project_id=task.project_id, user_id=user_id)
    if task.status != "paused":
        return ok_payload(request_id=request_id, data={"task": BatchGenerationTaskOut.model_validate(task).model_dump(), "resumed": False})

    failed_items = (
        db.execute(
            select(BatchGenerationTaskItem.chapter_number)
            .where(BatchGenerationTaskItem.task_id == task_id, BatchGenerationTaskItem.status == "failed")
            .order_by(BatchGenerationTaskItem.chapter_number.asc())
        )
        .scalars()
        .all()
    )
    if failed_items:
        raise AppError.conflict(
            message="当前批次存在失败章节，请先选择“重试失败章节”或“跳过失败章节”",
            details={"failed_chapter_numbers": [int(value) for value in failed_items]},
        )

    lock_and_enforce_batch_generation_quotas(
        db,
        project_id=str(task.project_id),
        user_id=str(task.actor_user_id or user_id),
        provider=str(task.runtime_provider or ""),
        ignore_task_id=task_id,
    )

    task.status = "queued"
    task.pause_requested = False
    task.cancel_requested = False
    task.error_json = None
    recalculate_batch_generation_counts(db, batch_task=task)
    requeue_batch_project_task(
        db,
        batch_task=task,
        event_type="resumed",
        source="batch_generation_resume",
        payload={"reason": "manual_resume"},
    )
    db.commit()
    _enqueue_batch_task_or_restore_paused(
        db,
        task=task,
        source="batch_generation_resume",
        reason="resume_enqueue_failed",
    )
    return ok_payload(request_id=request_id, data={**_build_batch_task_response(db, task_id=task_id), "resumed": True})


@router.post("/batch_generation_tasks/{task_id}/retry_failed")
def retry_failed_batch_generation_task(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    task_id: str,
) -> dict:
    request_id = request.state.request_id
    task = db.get(BatchGenerationTask, task_id)
    if task is None:
        raise AppError.not_found()
    require_project_editor(db, project_id=task.project_id, user_id=user_id)

    if task.status != "paused":
        return ok_payload(request_id=request_id, data={"task": BatchGenerationTaskOut.model_validate(task).model_dump(), "retried": False})

    enter_batch_generation_quota_admission(db)
    task = db.get(BatchGenerationTask, task_id)
    if task is None:
        raise AppError.not_found()
    require_project_editor(db, project_id=task.project_id, user_id=user_id)
    if task.status != "paused":
        return ok_payload(request_id=request_id, data={"task": BatchGenerationTaskOut.model_validate(task).model_dump(), "retried": False})

    failed_items = (
        db.execute(
            select(BatchGenerationTaskItem)
            .where(BatchGenerationTaskItem.task_id == task_id, BatchGenerationTaskItem.status == "failed")
            .order_by(BatchGenerationTaskItem.chapter_number.asc())
        )
        .scalars()
        .all()
    )
    if not failed_items:
        return ok_payload(request_id=request_id, data={"task": BatchGenerationTaskOut.model_validate(task).model_dump(), "retried": False})

    lock_and_enforce_batch_generation_quotas(
        db,
        project_id=str(task.project_id),
        user_id=str(task.actor_user_id or user_id),
        provider=str(task.runtime_provider or ""),
        ignore_task_id=task_id,
    )

    retried_numbers: list[int] = []
    for item in failed_items:
        retried_numbers.append(int(item.chapter_number))
        item.status = "queued"
        item.error_message = None
        item.last_error_json = None
        item.started_at = None
        item.finished_at = None
        append_batch_project_task_event(
            db,
            batch_task=task,
            event_type="step_requeued",
            source="batch_generation_retry_failed",
            payload={
                "reason": "retry_failed",
                "step": build_batch_step_payload(item),
                "checkpoint": build_batch_generation_checkpoint(task),
            },
        )

    task.status = "queued"
    task.pause_requested = False
    task.cancel_requested = False
    task.error_json = None
    recalculate_batch_generation_counts(db, batch_task=task)
    requeue_batch_project_task(
        db,
        batch_task=task,
        event_type="retry",
        source="batch_generation_retry_failed",
        payload={"reason": "retry_failed", "failed_chapter_numbers": retried_numbers},
    )
    db.commit()
    _enqueue_batch_task_or_restore_paused(
        db,
        task=task,
        source="batch_generation_retry_failed",
        reason="retry_failed_enqueue_failed",
    )
    return ok_payload(request_id=request_id, data={**_build_batch_task_response(db, task_id=task_id), "retried": True})


@router.post("/batch_generation_tasks/{task_id}/skip_failed")
def skip_failed_batch_generation_task(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    task_id: str,
) -> dict:
    request_id = request.state.request_id
    task = db.get(BatchGenerationTask, task_id)
    if task is None:
        raise AppError.not_found()
    require_project_editor(db, project_id=task.project_id, user_id=user_id)

    if task.status != "paused":
        return ok_payload(request_id=request_id, data={"task": BatchGenerationTaskOut.model_validate(task).model_dump(), "skipped": False})

    enter_batch_generation_quota_admission(db)
    task = db.get(BatchGenerationTask, task_id)
    if task is None:
        raise AppError.not_found()
    require_project_editor(db, project_id=task.project_id, user_id=user_id)
    if task.status != "paused":
        return ok_payload(request_id=request_id, data={"task": BatchGenerationTaskOut.model_validate(task).model_dump(), "skipped": False})

    failed_items = (
        db.execute(
            select(BatchGenerationTaskItem)
            .where(BatchGenerationTaskItem.task_id == task_id, BatchGenerationTaskItem.status == "failed")
            .order_by(BatchGenerationTaskItem.chapter_number.asc())
        )
        .scalars()
        .all()
    )
    if not failed_items:
        return ok_payload(request_id=request_id, data={"task": BatchGenerationTaskOut.model_validate(task).model_dump(), "skipped": False})

    pending_count = (
        db.execute(
            select(BatchGenerationTaskItem.id).where(
                BatchGenerationTaskItem.task_id == task_id,
                BatchGenerationTaskItem.status == "queued",
            )
        )
        .scalars()
        .all()
    )
    if pending_count:
        lock_and_enforce_batch_generation_quotas(
            db,
            project_id=str(task.project_id),
            user_id=str(task.actor_user_id or user_id),
            provider=str(task.runtime_provider or ""),
            ignore_task_id=task_id,
        )

    skipped_numbers: list[int] = []
    for item in failed_items:
        skipped_numbers.append(int(item.chapter_number))
        item.status = "skipped"
        item.finished_at = item.finished_at or utc_now()
        append_batch_project_task_event(
            db,
            batch_task=task,
            event_type="step_skipped",
            source="batch_generation_skip_failed",
            payload={
                "reason": "skip_failed",
                "step": build_batch_step_payload(item),
                "checkpoint": build_batch_generation_checkpoint(task),
            },
        )

    task.pause_requested = False
    task.cancel_requested = False
    task.error_json = None
    recalculate_batch_generation_counts(db, batch_task=task)

    if pending_count:
        task.status = "queued"
        requeue_batch_project_task(
            db,
            batch_task=task,
            event_type="resumed",
            source="batch_generation_skip_failed",
            payload={"reason": "skip_failed_resume", "skipped_chapter_numbers": skipped_numbers},
        )
        db.commit()
        _enqueue_batch_task_or_restore_paused(
            db,
            task=task,
            source="batch_generation_skip_failed",
            reason="skip_failed_enqueue_failed",
        )
    else:
        task.status = "succeeded"
        task.pause_requested = False
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
            payload={"reason": "skip_failed_completed", "skipped_chapter_numbers": skipped_numbers},
        )
        db.commit()

    return ok_payload(request_id=request_id, data={**_build_batch_task_response(db, task_id=task_id), "skipped": True})


@router.post("/batch_generation_tasks/{task_id}/cancel")
def cancel_batch_generation_task(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    task_id: str,
) -> dict:
    request_id = request.state.request_id
    task = db.get(BatchGenerationTask, task_id)
    if task is None:
        raise AppError.not_found()
    require_project_editor(db, project_id=task.project_id, user_id=user_id)

    if task.status not in ("queued", "running", "paused"):
        return ok_payload(request_id=request_id, data={"task": BatchGenerationTaskOut.model_validate(task).model_dump(), "canceled": False})

    if task.cancel_requested:
        return ok_payload(request_id=request_id, data={"task": BatchGenerationTaskOut.model_validate(task).model_dump(), "canceled": False})

    task.cancel_requested = True
    task.pause_requested = False
    sync_batch_generation_checkpoint(task)
    if task.status in ("queued", "paused"):
        immediate_cancel_reason = "manual_cancel" if task.status == "queued" else "manual_cancel_from_paused"
        task.status = "canceled"
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
            item.finished_at = item.finished_at or utc_now()
        recalculate_batch_generation_counts(db, batch_task=task)
        finalize_batch_project_task(
            db,
            batch_task=task,
            status="canceled",
            event_type="canceled",
            result={"canceled": True, "batch_task_id": str(task.id)},
            payload={"reason": immediate_cancel_reason},
        )
    elif task.project_task_id:
        runtime_task = db.get(ProjectTask, task.project_task_id)
        if runtime_task is not None:
            append_project_task_event(
                db,
                task=runtime_task,
                event_type="checkpoint",
                source="batch_generation_cancel",
                payload={
                    "reason": "manual_cancel_requested",
                    "checkpoint": build_batch_generation_checkpoint(task),
                },
            )

    db.commit()
    return ok_payload(request_id=request_id, data={"task": BatchGenerationTaskOut.model_validate(task).model_dump(), "canceled": True})
