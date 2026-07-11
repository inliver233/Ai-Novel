from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.utils import new_id, utc_now
from app.models.batch_generation_task import BatchGenerationTask, BatchGenerationTaskItem
from app.models.project_task import ProjectTask
from app.services.batch_generation_helpers import (
    append_batch_project_task_event,
    build_batch_generation_checkpoint,
    build_batch_step_payload,
    ensure_batch_generation_project_task,
    finalize_batch_project_task,
    pause_batch_generation,
    recalculate_batch_generation_counts,
    requeue_batch_project_task,
    sync_batch_generation_checkpoint,
)
from app.services.project_task_event_service import append_project_task_event
from app.services.task_queue import get_task_queue


ACTIVE_BATCH_GENERATION_STATUSES: Final[frozenset[str]] = frozenset({"queued", "running", "paused"})
TERMINAL_BATCH_GENERATION_STATUSES: Final[frozenset[str]] = frozenset({"succeeded", "failed", "canceled"})
BATCH_GENERATION_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "pause": frozenset({"queued", "running"}),
    "resume": frozenset({"paused"}),
    "retry_failed": frozenset({"paused"}),
    "skip_failed": frozenset({"paused"}),
    "cancel": ACTIVE_BATCH_GENERATION_STATUSES,
}


@dataclass(frozen=True, slots=True)
class BatchGenerationSnapshot:
    task: BatchGenerationTask
    items: tuple[BatchGenerationTaskItem, ...]


@dataclass(frozen=True, slots=True)
class BatchGenerationCommandResult:
    snapshot: BatchGenerationSnapshot
    applied: bool


def load_batch_generation_snapshot(db: Session, *, task_id: str) -> BatchGenerationSnapshot:
    db.expire_all()
    task = db.get(BatchGenerationTask, task_id)
    if task is None:
        raise AppError.not_found()
    items = tuple(
        db.execute(
            select(BatchGenerationTaskItem)
            .where(BatchGenerationTaskItem.task_id == task_id)
            .order_by(BatchGenerationTaskItem.chapter_number.asc())
        )
        .scalars()
        .all()
    )
    return BatchGenerationSnapshot(task=task, items=items)


def _begin_batch_task_mutation(db: Session) -> None:
    """Start a write-first transaction before reading mutable task state."""

    if db.new or db.dirty or db.deleted:
        raise RuntimeError("batch task mutation must begin before pending ORM mutations")
    if db.get_bind().dialect.name == "sqlite":
        db.rollback()
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    db.expire_all()


def _lock_batch_task(
    db: Session,
    *,
    task_id: str,
    authorized_project_id: str,
) -> BatchGenerationTask:
    _begin_batch_task_mutation(db)
    task = db.execute(
        select(BatchGenerationTask).where(BatchGenerationTask.id == task_id).with_for_update()
    ).scalar_one_or_none()
    if task is None:
        raise AppError.not_found()
    if str(task.project_id) != str(authorized_project_id):
        # Authorization was performed by the route against this immutable scope.
        # Refuse to mutate if the row no longer matches that authorization context.
        raise AppError.not_found()
    return task


def load_batch_generation_project_id(db: Session, *, task_id: str) -> str:
    project_id = db.execute(
        select(BatchGenerationTask.project_id).where(BatchGenerationTask.id == task_id)
    ).scalar_one_or_none()
    if project_id is None:
        raise AppError.not_found()
    return str(project_id)


def lock_batch_generation_task_for_worker(
    db: Session,
    *,
    task_id: str,
) -> BatchGenerationTask | None:
    _begin_batch_task_mutation(db)
    return db.execute(
        select(BatchGenerationTask).where(BatchGenerationTask.id == task_id).with_for_update()
    ).scalar_one_or_none()


def claim_batch_generation_task_for_worker(db: Session, *, task_id: str) -> bool:
    """Claim a queued delivery inside the caller's initialization transaction.

    Running tasks are deliberately never reclaimed here. Crash recovery needs a
    separately designed stale-running lease and is outside this claim contract.
    """

    _begin_batch_task_mutation(db)
    now = utc_now()
    result = db.execute(
        update(BatchGenerationTask)
        .where(
            BatchGenerationTask.id == task_id,
            BatchGenerationTask.status == "queued",
            BatchGenerationTask.cancel_requested.is_(False),
            BatchGenerationTask.pause_requested.is_(False),
        )
        .values(status="running", updated_at=now)
    )
    return bool(getattr(result, "rowcount", 0))


def claim_batch_generation_item_for_worker(
    db: Session,
    *,
    task_id: str,
    item_id: str,
    request_id: str,
) -> bool:
    """Claim one queued step without holding a transaction across external work."""

    _begin_batch_task_mutation(db)
    now = utc_now()
    runnable_task = exists(
        select(BatchGenerationTask.id).where(
            BatchGenerationTask.id == task_id,
            BatchGenerationTask.status == "running",
            BatchGenerationTask.cancel_requested.is_(False),
            BatchGenerationTask.pause_requested.is_(False),
        )
    )
    result = db.execute(
        update(BatchGenerationTaskItem)
        .where(
            BatchGenerationTaskItem.id == item_id,
            BatchGenerationTaskItem.task_id == task_id,
            BatchGenerationTaskItem.status == "queued",
            runnable_task,
        )
        .values(
            status="running",
            attempt_count=BatchGenerationTaskItem.attempt_count + 1,
            started_at=now,
            finished_at=None,
            last_request_id=request_id,
            last_error_json=None,
            error_message=None,
            updated_at=now,
        )
    )
    db.commit()
    return bool(getattr(result, "rowcount", 0))


def apply_batch_generation_worker_control(
    db: Session,
    *,
    task: BatchGenerationTask,
    cancel_reason: str,
    pause_reason: str,
    item: BatchGenerationTaskItem | None = None,
    payload: dict[str, object] | None = None,
) -> bool:
    """Honor control flags while the caller owns the task row lock."""

    if task.status in TERMINAL_BATCH_GENERATION_STATUSES or task.status == "paused":
        return True
    if task.cancel_requested:
        task.status = "canceled"
        task.pause_requested = False
        active_items = (
            db.execute(
                select(BatchGenerationTaskItem).where(
                    BatchGenerationTaskItem.task_id == str(task.id),
                    BatchGenerationTaskItem.status.in_(["queued", "running"]),
                )
            )
            .scalars()
            .all()
        )
        for active_item in active_items:
            active_item.status = "canceled"
            active_item.finished_at = active_item.finished_at or utc_now()
        recalculate_batch_generation_counts(db, batch_task=task)
        finalize_batch_project_task(
            db,
            batch_task=task,
            status="canceled",
            event_type="canceled",
            result={"canceled": True, "batch_task_id": str(task.id)},
            payload={**dict(payload or {}), "reason": cancel_reason},
        )
        return True
    if task.pause_requested:
        pause_batch_generation(
            db,
            batch_task=task,
            reason=pause_reason,
            source="batch_generation_worker",
            item=item,
            payload=payload,
        )
        return True
    return False


def _command_noop(db: Session, *, task: BatchGenerationTask) -> BatchGenerationCommandResult:
    db.rollback()
    return BatchGenerationCommandResult(
        snapshot=load_batch_generation_snapshot(db, task_id=str(task.id)),
        applied=False,
    )


def _commit_result(db: Session, *, task: BatchGenerationTask, applied: bool) -> BatchGenerationCommandResult:
    task_id = str(task.id)
    db.commit()
    return BatchGenerationCommandResult(
        snapshot=load_batch_generation_snapshot(db, task_id=task_id),
        applied=applied,
    )


def _queue_error(exc: Exception) -> tuple[AppError, dict[str, object]]:
    if isinstance(exc, AppError):
        return exc, {"code": exc.code, "message": exc.message, "details": exc.details}
    error = AppError(code="QUEUE_ENQUEUE_ERROR", message="批量生成任务入队失败", status_code=503)
    return error, {
        "code": error.code,
        "message": error.message,
        "details": {"error_type": type(exc).__name__},
    }


def _compensate_requeue_enqueue_failure(
    db: Session,
    *,
    task_id: str,
    authorized_project_id: str,
    source: str,
    reason: str,
    error: dict[str, object],
) -> None:
    task = _lock_batch_task(
        db,
        task_id=task_id,
        authorized_project_id=authorized_project_id,
    )
    if task.status == "queued" and not task.cancel_requested:
        append_batch_project_task_event(
            db,
            batch_task=task,
            event_type="checkpoint",
            source=source,
            payload={
                "reason": reason,
                "checkpoint": build_batch_generation_checkpoint(task),
                "error": error,
            },
        )
        pause_batch_generation(
            db,
            batch_task=task,
            reason=reason,
            source=source,
            error=error,
        )
        db.commit()
    else:
        db.rollback()


def _enqueue_requeued_task_or_compensate(
    db: Session,
    *,
    task_id: str,
    authorized_project_id: str,
    source: str,
    reason: str,
) -> None:
    try:
        get_task_queue().enqueue_batch_generation_task(task_id)
    except Exception as exc:
        error, error_payload = _queue_error(exc)
        _compensate_requeue_enqueue_failure(
            db,
            task_id=task_id,
            authorized_project_id=authorized_project_id,
            source=source,
            reason=reason,
            error=error_payload,
        )
        if error is exc:
            raise error
        raise error from exc


def _compensate_create_enqueue_failure(
    db: Session,
    *,
    task_id: str,
    authorized_project_id: str,
    error: dict[str, object],
) -> None:
    task = _lock_batch_task(
        db,
        task_id=task_id,
        authorized_project_id=authorized_project_id,
    )
    if task.status != "queued" or task.cancel_requested:
        db.rollback()
        return

    task.status = "failed"
    task.failed_count = max(int(task.failed_count or 0), 1)
    task.error_json = json.dumps(error, ensure_ascii=False)
    sync_batch_generation_checkpoint(task)
    items = (
        db.execute(
            select(BatchGenerationTaskItem).where(
                BatchGenerationTaskItem.task_id == task_id,
                BatchGenerationTaskItem.status == "queued",
            )
        )
        .scalars()
        .all()
    )
    for item in items:
        item.status = "failed"
        item.error_message = f"{error['message']} ({error['code']})"
    runtime_task = db.get(ProjectTask, task.project_task_id) if task.project_task_id else None
    if runtime_task is not None:
        runtime_task.status = "failed"
        runtime_task.error_json = json.dumps(error, ensure_ascii=False)
        append_project_task_event(
            db,
            task=runtime_task,
            event_type="failed",
            source="batch_generation_enqueue",
            payload={
                "reason": "enqueue_failed",
                "checkpoint": build_batch_generation_checkpoint(task),
                "error": error,
            },
        )
    db.commit()


def create_admitted_batch_generation_task(
    db: Session,
    *,
    project_id: str,
    outline_id: str,
    actor_user_id: str,
    runtime_provider: str,
    params_json: str,
    chapter_refs: list[tuple[str, int]],
    request_id: str | None,
) -> BatchGenerationSnapshot:
    """Persist an already quota-admitted batch and own its enqueue compensation."""

    task_id = new_id()
    task = BatchGenerationTask(
        id=task_id,
        project_id=project_id,
        outline_id=outline_id,
        actor_user_id=actor_user_id,
        runtime_provider=runtime_provider,
        status="queued",
        total_count=len(chapter_refs),
        completed_count=0,
        failed_count=0,
        skipped_count=0,
        cancel_requested=False,
        pause_requested=False,
        params_json=params_json,
        checkpoint_json=None,
        error_json=None,
    )
    items = [
        BatchGenerationTaskItem(
            id=new_id(),
            task_id=task_id,
            chapter_id=chapter_id,
            chapter_number=chapter_number,
            status="queued",
            generation_run_id=None,
            error_message=None,
        )
        for chapter_id, chapter_number in chapter_refs
    ]
    db.add(task)
    db.add_all(items)
    ensure_batch_generation_project_task(
        db,
        batch_task=task,
        chapter_numbers=[chapter_number for _chapter_id, chapter_number in chapter_refs],
        request_id=request_id,
    )
    db.commit()

    try:
        get_task_queue().enqueue_batch_generation_task(task_id)
    except Exception as exc:
        error, error_payload = _queue_error(exc)
        _compensate_create_enqueue_failure(
            db,
            task_id=task_id,
            authorized_project_id=project_id,
            error=error_payload,
        )
        if error is exc:
            raise error
        raise error from exc
    return load_batch_generation_snapshot(db, task_id=task_id)


def pause_batch_generation_task(
    db: Session,
    *,
    task_id: str,
    authorized_project_id: str,
) -> BatchGenerationCommandResult:
    task = _lock_batch_task(db, task_id=task_id, authorized_project_id=authorized_project_id)
    if task.status not in BATCH_GENERATION_TRANSITIONS["pause"]:
        return _command_noop(db, task=task)
    if task.pause_requested and task.status != "queued":
        return _command_noop(db, task=task)

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
    return _commit_result(db, task=task, applied=True)


def resume_batch_generation_task(
    db: Session,
    *,
    task_id: str,
    authorized_project_id: str,
    actor_user_id: str,
) -> BatchGenerationCommandResult:
    task = _lock_batch_task(db, task_id=task_id, authorized_project_id=authorized_project_id)
    if task.status not in BATCH_GENERATION_TRANSITIONS["resume"]:
        return _command_noop(db, task=task)

    failed_numbers = (
        db.execute(
            select(BatchGenerationTaskItem.chapter_number)
            .where(
                BatchGenerationTaskItem.task_id == task_id,
                BatchGenerationTaskItem.status == "failed",
            )
            .order_by(BatchGenerationTaskItem.chapter_number.asc())
        )
        .scalars()
        .all()
    )
    if failed_numbers:
        db.rollback()
        raise AppError.conflict(
            message="当前批次存在失败章节，请先选择“重试失败章节”或“跳过失败章节”",
            details={"failed_chapter_numbers": [int(value) for value in failed_numbers]},
        )

    from app.services.batch_generation_quota import lock_and_enforce_batch_generation_quotas

    lock_and_enforce_batch_generation_quotas(
        db,
        project_id=str(task.project_id),
        user_id=str(task.actor_user_id or actor_user_id),
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
    _enqueue_requeued_task_or_compensate(
        db,
        task_id=task_id,
        authorized_project_id=authorized_project_id,
        source="batch_generation_resume",
        reason="resume_enqueue_failed",
    )
    return BatchGenerationCommandResult(snapshot=load_batch_generation_snapshot(db, task_id=task_id), applied=True)


def retry_failed_batch_generation_task(
    db: Session,
    *,
    task_id: str,
    authorized_project_id: str,
    actor_user_id: str,
) -> BatchGenerationCommandResult:
    task = _lock_batch_task(db, task_id=task_id, authorized_project_id=authorized_project_id)
    if task.status not in BATCH_GENERATION_TRANSITIONS["retry_failed"]:
        return _command_noop(db, task=task)

    failed_items = (
        db.execute(
            select(BatchGenerationTaskItem)
            .where(
                BatchGenerationTaskItem.task_id == task_id,
                BatchGenerationTaskItem.status == "failed",
            )
            .order_by(BatchGenerationTaskItem.chapter_number.asc())
        )
        .scalars()
        .all()
    )
    if not failed_items:
        return _command_noop(db, task=task)

    from app.services.batch_generation_quota import lock_and_enforce_batch_generation_quotas

    lock_and_enforce_batch_generation_quotas(
        db,
        project_id=str(task.project_id),
        user_id=str(task.actor_user_id or actor_user_id),
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
    _enqueue_requeued_task_or_compensate(
        db,
        task_id=task_id,
        authorized_project_id=authorized_project_id,
        source="batch_generation_retry_failed",
        reason="retry_failed_enqueue_failed",
    )
    return BatchGenerationCommandResult(snapshot=load_batch_generation_snapshot(db, task_id=task_id), applied=True)


def skip_failed_batch_generation_task(
    db: Session,
    *,
    task_id: str,
    authorized_project_id: str,
    actor_user_id: str,
) -> BatchGenerationCommandResult:
    task = _lock_batch_task(db, task_id=task_id, authorized_project_id=authorized_project_id)
    if task.status not in BATCH_GENERATION_TRANSITIONS["skip_failed"]:
        return _command_noop(db, task=task)

    failed_items = (
        db.execute(
            select(BatchGenerationTaskItem)
            .where(
                BatchGenerationTaskItem.task_id == task_id,
                BatchGenerationTaskItem.status == "failed",
            )
            .order_by(BatchGenerationTaskItem.chapter_number.asc())
        )
        .scalars()
        .all()
    )
    if not failed_items:
        return _command_noop(db, task=task)
    has_pending_items = (
        db.execute(
            select(BatchGenerationTaskItem.id)
            .where(
                BatchGenerationTaskItem.task_id == task_id,
                BatchGenerationTaskItem.status == "queued",
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )

    if has_pending_items:
        from app.services.batch_generation_quota import lock_and_enforce_batch_generation_quotas

        lock_and_enforce_batch_generation_quotas(
            db,
            project_id=str(task.project_id),
            user_id=str(task.actor_user_id or actor_user_id),
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
    if has_pending_items:
        task.status = "queued"
        requeue_batch_project_task(
            db,
            batch_task=task,
            event_type="resumed",
            source="batch_generation_skip_failed",
            payload={"reason": "skip_failed_resume", "skipped_chapter_numbers": skipped_numbers},
        )
        db.commit()
        _enqueue_requeued_task_or_compensate(
            db,
            task_id=task_id,
            authorized_project_id=authorized_project_id,
            source="batch_generation_skip_failed",
            reason="skip_failed_enqueue_failed",
        )
    else:
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
                "failed_count": int(task.failed_count or 0),
                "skipped_count": int(task.skipped_count or 0),
            },
            payload={"reason": "skip_failed_completed", "skipped_chapter_numbers": skipped_numbers},
        )
        db.commit()
    return BatchGenerationCommandResult(snapshot=load_batch_generation_snapshot(db, task_id=task_id), applied=True)


def cancel_batch_generation_task(
    db: Session,
    *,
    task_id: str,
    authorized_project_id: str,
) -> BatchGenerationCommandResult:
    task = _lock_batch_task(db, task_id=task_id, authorized_project_id=authorized_project_id)
    if task.status not in BATCH_GENERATION_TRANSITIONS["cancel"] or task.cancel_requested:
        return _command_noop(db, task=task)

    original_status = str(task.status)
    task.cancel_requested = True
    task.pause_requested = False
    sync_batch_generation_checkpoint(task)
    if original_status in {"queued", "paused"}:
        reason = "manual_cancel" if original_status == "queued" else "manual_cancel_from_paused"
        task.status = "canceled"
        items = (
            db.execute(
                select(BatchGenerationTaskItem).where(
                    BatchGenerationTaskItem.task_id == task_id,
                    BatchGenerationTaskItem.status.in_(["queued", "running"]),
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
            payload={"reason": reason},
        )
    else:
        append_batch_project_task_event(
            db,
            batch_task=task,
            event_type="checkpoint",
            source="batch_generation_cancel",
            payload={
                "reason": "manual_cancel_requested",
                "checkpoint": build_batch_generation_checkpoint(task),
            },
        )
    return _commit_result(db, task=task, applied=True)
