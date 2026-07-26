from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update

from app.core.config import settings
from app.core.logging import exception_log_fields, log_event
from app.db.datetime_compat import coerce_utc_datetime
from app.db.session import SessionLocal
from app.db.utils import utc_now
from app.models.project_task import ProjectTask
from app.services.project_task_event_service import append_project_task_event
from app.services.task_queue import get_task_queue, project_task_queue_has_task

logger = logging.getLogger("ainovel")


@dataclass(slots=True)
class ProjectTaskHeartbeatHandle:
    stop_event: threading.Event
    thread: threading.Thread


@dataclass(slots=True)
class ProjectTaskWatchdogHandle:
    stop_event: threading.Event
    thread: threading.Thread


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _heartbeat_interval_seconds() -> int:
    value = int(getattr(settings, "project_task_heartbeat_interval_seconds", 5) or 5)
    return 1 if value <= 0 else value


def _watchdog_interval_seconds() -> int:
    value = int(getattr(settings, "project_task_watchdog_interval_seconds", 15) or 15)
    return 1 if value <= 0 else value


def _stale_running_timeout_seconds() -> int:
    value = int(getattr(settings, "project_task_stale_running_timeout_seconds", 120) or 120)
    return 30 if value <= 0 else value


def _queued_reconcile_after_seconds() -> int:
    value = int(getattr(settings, "project_task_queued_reconcile_after_seconds", 20) or 20)
    return 5 if value <= 0 else value


def _normalize_dt(value):
    return coerce_utc_datetime(value)


def touch_project_task_heartbeat(*, task_id: str) -> bool:
    db = SessionLocal()
    try:
        now = utc_now()
        res = db.execute(
            update(ProjectTask)
            .where(ProjectTask.id == task_id, ProjectTask.status == "running")
            .values(heartbeat_at=now, updated_at=now)
        )
        db.commit()
        return bool(getattr(res, "rowcount", 0))
    finally:
        db.close()


def start_project_task_heartbeat(*, task_id: str) -> ProjectTaskHeartbeatHandle:
    stop_event = threading.Event()

    def _run() -> None:
        interval_seconds = _heartbeat_interval_seconds()
        while not stop_event.wait(interval_seconds):
            try:
                if not touch_project_task_heartbeat(task_id=task_id):
                    return
            except Exception as exc:
                log_event(
                    logger,
                    "warning",
                    event="PROJECT_TASK_HEARTBEAT_ERROR",
                    task_id=task_id,
                    error_type=type(exc).__name__,
                    **exception_log_fields(exc),
                )

    thread = threading.Thread(target=_run, name=f"ainovel-project-task-heartbeat-{task_id}", daemon=True)
    thread.start()
    return ProjectTaskHeartbeatHandle(stop_event=stop_event, thread=thread)


def stop_project_task_heartbeat(handle: ProjectTaskHeartbeatHandle | None) -> None:
    if handle is None:
        return
    handle.stop_event.set()
    handle.thread.join(timeout=1.0)


def reconcile_project_tasks_once(*, reason: str, now=None) -> dict[str, int]:
    now_dt = now or utc_now()
    now_cmp = _normalize_dt(now_dt)
    stale_before = now_cmp - timedelta(seconds=_stale_running_timeout_seconds())
    queued_before = now_cmp - timedelta(seconds=_queued_reconcile_after_seconds())
    summary = {
        "timed_out_running": 0,
        "requeued_orphans": 0,
        "skipped_queue_unknown": 0,
        "enqueue_failures": 0,
        "stale_batch_generation": 0,
    }

    db = SessionLocal()
    try:
        stale_running = (
            db.execute(
                select(ProjectTask)
                .where(ProjectTask.status == "running")
                .order_by(ProjectTask.updated_at.asc(), ProjectTask.id.asc())
            )
            .scalars()
            .all()
        )
        for task in stale_running:
            reference = _normalize_dt(task.heartbeat_at or task.started_at or task.updated_at or task.created_at)
            if reference is None or reference > stale_before:
                continue
            task.status = "failed"
            task.result_json = None
            task.finished_at = now_dt
            task.heartbeat_at = now_dt
            task.updated_at = now_dt
            task.error_json = _json_dumps(
                {
                    "error_type": "ProjectTaskWatchdog",
                    "code": "PROJECT_TASK_HEARTBEAT_TIMEOUT",
                    "message": "ProjectTask heartbeat timed out and was failed by watchdog",
                    "details": {
                        "reason": reason,
                        "timeout_seconds": _stale_running_timeout_seconds(),
                        "last_heartbeat_at": reference.isoformat().replace("+00:00", "Z"),
                    },
                }
            )
            append_project_task_event(
                db,
                task=task,
                event_type="timeout",
                source=reason,
                payload={
                    "action": "mark_failed",
                    "reason": "heartbeat_timeout",
                    "timeout_seconds": _stale_running_timeout_seconds(),
                },
            )
            db.commit()
            summary["timed_out_running"] += 1

        queued_rows = (
            db.execute(
                select(ProjectTask)
                .where(ProjectTask.status == "queued")
                .order_by(ProjectTask.created_at.asc(), ProjectTask.id.asc())
            )
            .scalars()
            .all()
        )
        for task in queued_rows:
            present = project_task_queue_has_task(task_id=str(task.id))
            if present is True:
                continue
            if present is None:
                summary["skipped_queue_unknown"] += 1
                continue
            created_cmp = _normalize_dt(task.created_at)
            if created_cmp is None or created_cmp > queued_before:
                continue
            try:
                get_task_queue().enqueue(kind="project_task", task_id=str(task.id))
            except Exception as exc:
                summary["enqueue_failures"] += 1
                log_event(
                    logger,
                    "warning",
                    event="PROJECT_TASK_RECONCILE_ENQUEUE_ERROR",
                    task_id=str(task.id),
                    project_id=str(task.project_id),
                    reason=reason,
                    error_type=type(exc).__name__,
                    **exception_log_fields(exc),
                )
                continue
            append_project_task_event(
                db,
                task=task,
                event_type="reconcile",
                source=reason,
                payload={
                    "action": "re_enqueue_orphan",
                    "reason": "queue_missing",
                },
            )
            db.commit()
            summary["requeued_orphans"] += 1
    finally:
        db.close()

    # 批任务 stale-running 租约回收（backend-api#6）。恢复失败不阻断 ProjectTask 巡检。
    try:
        # 函数级延迟导入避免 batch_generation_helpers -> 本模块 -> commands 的环。
        from app.services.batch_generation_commands import recover_stale_running_batch_generation_tasks

        batch_db = SessionLocal()
        try:
            summary["stale_batch_generation"] = recover_stale_running_batch_generation_tasks(batch_db, now=now_dt)
        finally:
            batch_db.close()
    except Exception as exc:
        log_event(
            logger,
            "warning",
            event="BATCH_GENERATION_RECONCILE_ERROR",
            reason=reason,
            error_type=type(exc).__name__,
            **exception_log_fields(exc),
        )

    if any(summary.values()):
        log_event(logger, "info", event="PROJECT_TASK_RECONCILE", reason=reason, **summary)
    return summary


def start_project_task_watchdog() -> ProjectTaskWatchdogHandle | None:
    enabled = bool(getattr(settings, "project_task_watchdog_enabled", True))
    if not enabled:
        return None

    reconcile_project_tasks_once(reason="startup")
    stop_event = threading.Event()

    def _run() -> None:
        interval_seconds = _watchdog_interval_seconds()
        while not stop_event.wait(interval_seconds):
            try:
                reconcile_project_tasks_once(reason="watchdog")
            except Exception as exc:
                log_event(
                    logger,
                    "error",
                    event="PROJECT_TASK_WATCHDOG_ERROR",
                    error_type=type(exc).__name__,
                    **exception_log_fields(exc),
                )

    thread = threading.Thread(target=_run, name="ainovel-project-task-watchdog", daemon=True)
    thread.start()
    return ProjectTaskWatchdogHandle(stop_event=stop_event, thread=thread)


def stop_project_task_watchdog(handle: ProjectTaskWatchdogHandle | None) -> None:
    if handle is None:
        return
    handle.stop_event.set()
    handle.thread.join(timeout=1.0)
