from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.logging import exception_log_fields, log_event, redact_secrets_text
from app.core.secrets import redact_api_keys
from app.db.session import SessionLocal
from app.db.utils import new_id, utc_now
from app.models.project_task import ProjectTask
from app.services.project_task_event_service import (
    append_project_task_event,
    mark_project_task_enqueue_failed,
    reset_project_task_to_queued,
)
from app.services.project_task_runtime_service import start_project_task_heartbeat, stop_project_task_heartbeat

logger = logging.getLogger("ainovel")


_ALLOWED_TASK_STATUSES_QUERY = {"queued", "running", "paused", "failed", "done", "succeeded", "canceled"}
_TASK_DONE_ALIASES = {"succeeded", "done"}


def _compact_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _compact_json_loads(value: str | None) -> Any | None:
    if value is None:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    s = dt.isoformat()
    return s.replace("+00:00", "Z")


def _parse_dt(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _task_status_to_public(status: str) -> str:
    s = str(status or "").strip().lower()
    return "done" if s in _TASK_DONE_ALIASES else s


def _task_error_fields(task: ProjectTask) -> tuple[str | None, str | None]:
    value = _compact_json_loads(task.error_json) if task.error_json else None
    if not isinstance(value, dict):
        return None, None
    error_type = str(value.get("error_type") or "").strip() or None
    error_message = str(value.get("message") or "").strip() or None
    return error_type, error_message


def project_task_to_dict(*, task: ProjectTask, include_payloads: bool) -> dict[str, Any]:
    error_type, error_message = _task_error_fields(task)

    data: dict[str, Any] = {
        "id": str(task.id),
        "project_id": str(task.project_id),
        "actor_user_id": task.actor_user_id,
        "kind": str(task.kind),
        "status": _task_status_to_public(str(task.status)),
        "idempotency_key": str(getattr(task, "idempotency_key", "") or ""),
        "attempt": int(getattr(task, "attempt", 0) or 0),
        "error_type": error_type,
        "error_message": error_message,
        "timings": {
            "created_at": _iso(task.created_at),
            "started_at": _iso(task.started_at),
            "heartbeat_at": _iso(getattr(task, "heartbeat_at", None)),
            "finished_at": _iso(task.finished_at),
            "updated_at": _iso(task.updated_at),
        },
    }

    if include_payloads:
        params = _compact_json_loads(task.params_json) if task.params_json else None
        result = _compact_json_loads(task.result_json) if task.result_json else None
        err = _compact_json_loads(task.error_json) if task.error_json else None
        data["params"] = redact_api_keys(params) if params is not None else None
        data["result"] = redact_api_keys(result) if result is not None else None
        data["error"] = redact_api_keys(err) if err is not None else None

    return data


def _emit_and_enqueue_project_task(
    *,
    db: Session,
    task: ProjectTask,
    request_id: str | None,
    event_type: str | None,
    source: str,
    payload: dict[str, Any] | None = None,
) -> str:
    if event_type is not None:
        append_project_task_event(db, task=task, event_type=event_type, source=source, payload=payload)
        db.commit()

    from app.services.task_queue import get_task_queue

    queue = get_task_queue()
    try:
        queue.enqueue(kind="project_task", task_id=str(task.id))
    except Exception as exc:
        mark_project_task_enqueue_failed(db, task=task, exc=exc, logger=logger, request_id=request_id)
    return str(task.id)


def list_project_tasks(
    *,
    db: Session,
    project_id: str,
    status: str | None,
    kind: str | None,
    before: str | None,
    limit: int,
) -> dict[str, Any]:
    status_norm = str(status or "").strip().lower() or None
    if status_norm is not None:
        if status_norm == "succeeded":
            status_norm = "done"
        if status_norm not in _ALLOWED_TASK_STATUSES_QUERY:
            raise AppError.validation(details={"reason": "invalid_status", "status": status})

    kind_norm = str(kind or "").strip() or None

    before_raw = str(before or "").strip()
    before_dt = _parse_dt(before_raw) if before_raw else None
    if before_raw and before_dt is None:
        raise AppError.validation(details={"reason": "invalid_before", "before": before})

    q = select(ProjectTask).where(ProjectTask.project_id == project_id)
    if status_norm is not None:
        if status_norm == "done":
            q = q.where(ProjectTask.status.in_(sorted(_TASK_DONE_ALIASES)))
        else:
            q = q.where(ProjectTask.status == status_norm)
    if kind_norm is not None:
        q = q.where(ProjectTask.kind == kind_norm)
    if before_dt is not None:
        q = q.where(ProjectTask.created_at < before_dt)

    rows = db.execute(q.order_by(ProjectTask.created_at.desc(), ProjectTask.id.desc()).limit(limit + 1)).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    items = [project_task_to_dict(task=t, include_payloads=False) for t in rows]
    next_before = _iso(rows[-1].created_at) if (has_more and rows) else None
    return {"items": items, "next_before": next_before}


def _task_params_reason(params_json: str | None) -> str:
    if not params_json:
        return ""
    try:
        value = json.loads(params_json)
    except Exception:
        return ""
    if not isinstance(value, dict):
        return ""
    return str(value.get("reason") or "").strip()


def _since_prefix(idempotency_key: str) -> str | None:
    key = str(idempotency_key or "").strip()
    if not key:
        return None
    marker = ":since:"
    if marker not in key:
        return None
    return key.split(marker, 1)[0] + marker


def _dedupe_queued_chapter_tasks(*, db: Session, project_id: str, keep_task: ProjectTask) -> int:
    """
    Reduce task storms for chapter_done triggers:
    keep the latest queued task for a given idempotency prefix (`...:since:`) and cancel older queued tasks.

    Safety:
    - only cancels queued tasks
    - only cancels tasks whose params_json.reason starts with "chapter" (avoid canceling manual tasks)
    """

    pid = str(project_id or "").strip()
    if not pid:
        return 0

    keep_key = str(getattr(keep_task, "idempotency_key", "") or "").strip()
    prefix = _since_prefix(keep_key)
    if not prefix:
        return 0

    rows = (
        db.execute(
            select(ProjectTask).where(
                ProjectTask.project_id == pid,
                ProjectTask.status == "queued",
                ProjectTask.idempotency_key.like(f"{prefix}%"),
            )
        )
        .scalars()
        .all()
    )

    now = utc_now()
    canceled = 0
    for t in rows:
        if str(t.id) == str(getattr(keep_task, "id", "")):
            continue
        if str(getattr(t, "idempotency_key", "") or "").strip() == keep_key:
            continue
        reason = _task_params_reason(t.params_json).lower()
        if not reason.startswith("chapter"):
            continue
        t.status = "canceled"
        t.heartbeat_at = None
        t.finished_at = now
        t.updated_at = now
        t.result_json = _compact_json_dumps({"canceled": True, "reason": "deduped_by_newer_trigger"})
        t.error_json = None
        append_project_task_event(
            db,
            task=t,
            event_type="canceled",
            source="dedupe",
            payload={
                "reason": "deduped_by_newer_trigger",
                "replaced_by_task_id": str(getattr(keep_task, "id", "") or ""),
            },
        )
        canceled += 1

    if canceled:
        db.commit()
    return canceled


def _try_dedupe_queued_chapter_tasks(*, db: Session, project_id: str, keep_task_id: str | None) -> None:
    if not keep_task_id:
        return
    try:
        keep = db.get(ProjectTask, str(keep_task_id))
        if keep is None:
            return
        _dedupe_queued_chapter_tasks(db=db, project_id=project_id, keep_task=keep)
    except Exception:
        # fail-soft
        return


def schedule_chapter_done_tasks(
    *,
    db: Session,
    project_id: str,
    actor_user_id: str | None,
    request_id: str | None,
    chapter_id: str,
    chapter_token: str | None,
    reason: str,
) -> dict[str, str | None]:
    """
    Fail-soft scheduler bundle for chapter status transition -> done.

    Schedules:
    - ProjectTask(kind=vector_rebuild)
    - ProjectTask(kind=search_rebuild)
    - ProjectTask(kind=characters_auto_update)

    All schedulers are idempotent; this helper never raises.
    """

    pid = str(project_id or "").strip()
    cid = str(chapter_id or "").strip()
    reason_norm = str(reason or "").strip() or "chapter_done"
    token_norm = str(chapter_token or "").strip() or utc_now().isoformat().replace("+00:00", "Z")

    out: dict[str, str | None] = {
        "vector_rebuild": None,
        "search_rebuild": None,
        "characters_auto_update": None,
    }

    if not pid or not cid:
        return out

    from app.models.project_settings import ProjectSettings

    settings_row = db.get(ProjectSettings, pid)
    auto_characters = (
        bool(getattr(settings_row, "auto_update_characters_enabled", True)) if settings_row is not None else True
    )
    auto_vector = bool(getattr(settings_row, "auto_update_vector_enabled", True)) if settings_row is not None else True
    auto_search = bool(getattr(settings_row, "auto_update_search_enabled", True)) if settings_row is not None else True

    try:
        from app.services.vector_rag_service import schedule_vector_rebuild_task

        if auto_vector:
            out["vector_rebuild"] = schedule_vector_rebuild_task(
                db=db,
                project_id=pid,
                actor_user_id=actor_user_id,
                request_id=request_id,
                reason=reason_norm,
            )
    except Exception as exc:
        log_event(
            logger,
            "warning",
            event="CHAPTER_DONE_TASK_SCHEDULE_ERROR",
            project_id=pid,
            chapter_id=cid,
            kind="vector_rebuild",
            error_type=type(exc).__name__,
            **exception_log_fields(exc),
        )

    try:
        from app.services.search_index_service import schedule_search_rebuild_task

        if auto_search:
            out["search_rebuild"] = schedule_search_rebuild_task(
                db=db,
                project_id=pid,
                actor_user_id=actor_user_id,
                request_id=request_id,
                reason=reason_norm,
            )
    except Exception as exc:
        log_event(
            logger,
            "warning",
            event="CHAPTER_DONE_TASK_SCHEDULE_ERROR",
            project_id=pid,
            chapter_id=cid,
            kind="search_rebuild",
            error_type=type(exc).__name__,
            **exception_log_fields(exc),
        )

    if auto_characters:
        try:
            from app.services.characters_auto_update_service import schedule_characters_auto_update_task

            out["characters_auto_update"] = schedule_characters_auto_update_task(
                db=db,
                project_id=pid,
                actor_user_id=actor_user_id,
                request_id=request_id,
                chapter_id=cid,
                chapter_token=token_norm,
                reason=reason_norm,
            )
            if isinstance(db, Session):
                _try_dedupe_queued_chapter_tasks(db=db, project_id=pid, keep_task_id=out.get("characters_auto_update"))
        except Exception as exc:
            log_event(
                logger,
                "warning",
                event="CHAPTER_DONE_TASK_SCHEDULE_ERROR",
                project_id=pid,
                chapter_id=cid,
                kind="characters_auto_update",
                error_type=type(exc).__name__,
                **exception_log_fields(exc),
            )

    return out


def retry_project_task(*, db: Session, task: ProjectTask) -> ProjectTask:
    """
    Idempotent retry for failed ProjectTask.

    Note: actual enqueue/worker execution is handled by the queue backend / worker entrypoint.
    """

    status_norm = str(getattr(task, "status", "") or "").strip().lower()
    if status_norm != "failed":
        return task

    reset_project_task_to_queued(task=task, increment_retry_count=True)
    db.commit()

    _emit_and_enqueue_project_task(
        db=db,
        task=task,
        request_id=None,
        event_type="retry",
        source="manual_retry",
        payload={"reason": "manual_retry"},
    )
    return task


def cancel_project_task(*, db: Session, task: ProjectTask) -> ProjectTask:
    """
    Cancel a queued ProjectTask.

    Contract:
    - Only queued tasks are cancelable (idempotent no-op otherwise).
    - Worker must skip execution when task.status == "canceled".
    """

    status_norm = str(getattr(task, "status", "") or "").strip().lower()
    if status_norm != "queued":
        return task

    task.status = "canceled"
    task.started_at = None
    task.heartbeat_at = None
    task.finished_at = utc_now()
    task.updated_at = utc_now()
    task.result_json = _compact_json_dumps({"canceled": True})
    task.error_json = None
    append_project_task_event(
        db, task=task, event_type="canceled", source="manual_cancel", payload={"reason": "manual_cancel"}
    )
    db.commit()
    return task


def run_project_task(*, task_id: str) -> str:
    """
    RQ worker entrypoint. Consumes ProjectTask and records result to DB.
    """

    db = SessionLocal()
    try:
        task = db.get(ProjectTask, task_id)
        if task is None:
            log_event(logger, "warning", event="PROJECT_TASK_MISSING", task_id=task_id)
            return task_id

        status_norm = str(getattr(task, "status", "") or "").strip().lower()
        if status_norm in {"succeeded", "done", "failed", "running", "paused"}:
            return task_id
        if status_norm == "canceled":
            if task.finished_at is None:
                task.finished_at = utc_now()
                task.updated_at = utc_now()
                db.commit()
            return task_id

        if status_norm != "queued":
            return task_id

        started_at = utc_now()
        res = db.execute(
            update(ProjectTask)
            .where(ProjectTask.id == task_id, ProjectTask.status == "queued")
            .values(
                status="running",
                started_at=started_at,
                heartbeat_at=started_at,
                attempt=ProjectTask.attempt + 1,
                updated_at=started_at,
            )
        )
        db.commit()
        if not getattr(res, "rowcount", 0):
            return task_id
        task = db.get(ProjectTask, task_id)
        if task is None:
            return task_id
        append_project_task_event(
            db, task=task, event_type="running", source="worker", payload={"reason": "worker_start"}
        )
        db.commit()
        heartbeat_handle = start_project_task_heartbeat(task_id=task_id)

        kind = str(task.kind)
        project_id = str(task.project_id)

        result: dict[str, Any]
        if kind == "noop":
            result = {"skipped": True, "note": "noop"}
        elif kind == "search_rebuild":
            from app.services.search_index_service import rebuild_project_search_index_async

            result = rebuild_project_search_index_async(project_id=project_id)
        elif kind == "characters_auto_update":
            params = _compact_json_loads(task.params_json) if task.params_json else None
            params_dict = params if isinstance(params, dict) else {}
            chapter_id = str(params_dict.get("chapter_id") or "").strip()
            request_id2 = str(params_dict.get("request_id") or "").strip() or None
            actor_user_id = str(getattr(task, "actor_user_id", "") or "").strip()
            if not actor_user_id:
                raise AppError(
                    code="PROJECT_TASK_CONFIG_ERROR",
                    message="characters_auto_update 缺少 actor_user_id（无法解析 API Key）",
                    status_code=500,
                    details={
                        "task_kind": "characters_auto_update",
                        "how_to_fix": [
                            "通过 UI 触发任务时，确保已登录且具备 editor 权限",
                            "如果是系统触发（无 user），请改为传入明确的 actor_user_id 或配置项目级 API Key",
                        ],
                    },
                )
            if not chapter_id:
                raise ValueError("Missing ProjectTask.params_json.chapter_id for characters_auto_update")

            from app.services.characters_auto_update_service import characters_auto_update_v1

            res = characters_auto_update_v1(
                project_id=project_id,
                actor_user_id=actor_user_id,
                request_id=request_id2 or f"project_task:{task_id}",
                chapter_id=chapter_id,
            )
            if not bool(res.get("ok")):
                reason = str(res.get("reason") or "unknown").strip() or "unknown"
                run_id = str(res.get("run_id") or "").strip() or None
                error_type2 = str(res.get("error_type") or "").strip() or None
                error_message2 = str(res.get("error_message") or "").strip() or None
                parse_error = res.get("parse_error") if isinstance(res.get("parse_error"), dict) else None
                attempts = res.get("attempts") if isinstance(res.get("attempts"), list) else None
                error_obj = res.get("error") if isinstance(res.get("error"), dict) else None

                how_to_fix: list[str] = []
                if reason == "api_key_missing":
                    how_to_fix = [
                        "在「模型配置/项目设置」中配置可用的 API Key（或检查请求头 X-LLM-API-Key）",
                        "确认当前项目已绑定 LLM Profile / Preset（用于 characters_auto_update）",
                    ]
                elif reason == "llm_preset_missing":
                    how_to_fix = ["先在项目中选择/绑定可用的 LLM Profile，并刷新页面后重试任务"]
                elif reason == "llm_call_failed":
                    how_to_fix = [
                        "检查 base_url / 网络连通性（可用「模型配置 → 测试连接」验证）",
                        "确认模型与参数兼容；必要时切换 provider/model 后重试",
                    ]
                elif reason == "parse_error":
                    how_to_fix = [
                        "模型输出未满足 JSON 合同：可在任务详情中查看 run_id 并定位输出",
                        "尝试更换模型/降低温度后重试",
                    ]
                elif reason == "apply_failed":
                    how_to_fix = ["数据库写入失败：请查看 error.details 或 backend.log；修复后重试任务"]

                details: dict[str, Any] = {
                    "task_kind": "characters_auto_update",
                    "reason": reason,
                    "run_id": run_id,
                    "error_type": error_type2,
                    "error_message": error_message2,
                    "parse_error": parse_error,
                }
                if attempts is not None:
                    details["attempts"] = attempts
                if error_obj is not None:
                    details["error"] = error_obj
                if how_to_fix:
                    details["how_to_fix"] = how_to_fix

                msg = f"characters_auto_update 失败：{reason}"
                if run_id:
                    msg += f" (run_id={run_id})"
                if error_message2:
                    msg += f" - {error_message2[:160]}"

                raise AppError(code="CHARACTERS_AUTO_UPDATE_FAILED", message=msg, status_code=500, details=details)
            result = res
        elif kind == "vector_rebuild":
            from app.models.project_settings import ProjectSettings
            from app.services.vector_embedding_overrides import vector_embedding_overrides
            from app.services.vector_kb_service import ensure_default_kb as ensure_default_vector_kb
            from app.services.vector_rag_service import (
                rebuild_kb_vectors,
                vector_rag_status,
            )

            db2 = SessionLocal()
            embedding: dict[str, str | None] = {}
            build_revision = 0
            try:
                settings_row = db2.get(ProjectSettings, project_id)
                build_revision = int(getattr(settings_row, "vector_dirty_revision", 0) or 0)
                embedding = vector_embedding_overrides(settings_row)
                ensure_default_vector_kb(db2, project_id=project_id)
                status = vector_rag_status(project_id=project_id, embedding=embedding)
                if not bool(status.get("enabled")):
                    result = {"skipped": True, **status}
                else:
                    result = rebuild_kb_vectors(
                        db=db2,
                        project_id=project_id,
                        kb_ids=["default"],
                        embedding=embedding,
                    )
                    result["build_revision"] = build_revision
            finally:
                db2.close()

            if not result:
                result = {"enabled": False, "skipped": True, "build_revision": build_revision}
            else:
                result.setdefault("build_revision", build_revision)

            if bool(result.get("enabled")) and not bool(result.get("skipped")):
                cleared = db.execute(
                    update(ProjectSettings)
                    .where(
                        ProjectSettings.project_id == project_id,
                        ProjectSettings.vector_dirty_revision == build_revision,
                    )
                    .values(vector_index_dirty=False, last_vector_build_at=utc_now())
                )
                revision_matched = bool(getattr(cleared, "rowcount", 0))
                if revision_matched:
                    result["stale"] = False
                else:
                    current_revision = int(
                        db.execute(
                            select(ProjectSettings.vector_dirty_revision).where(
                                ProjectSettings.project_id == project_id
                            )
                        ).scalar_one_or_none()
                        or 0
                    )
                    result.update({"stale": True, "current_dirty_revision": current_revision})

        else:
            raise ValueError(f"Unsupported ProjectTask.kind: {kind!r}")

        if kind == "vector_rebuild" and bool(result.get("stale")):
            params = _compact_json_loads(task.params_json) if task.params_json else None
            params_dict = dict(params) if isinstance(params, dict) else {}
            params_dict.update(
                {
                    "reason": "revision_advanced_during_rebuild",
                    "dirty_revision": int(result.get("current_dirty_revision") or 0),
                    "requeued_at": utc_now().isoformat().replace("+00:00", "Z"),
                }
            )
            task.status = "queued"
            task.params_json = _compact_json_dumps(params_dict)
            task.result_json = None
            task.error_json = None
            task.started_at = None
            task.heartbeat_at = None
            task.finished_at = None
            append_project_task_event(
                db,
                task=task,
                event_type="queued",
                source="worker",
                payload={"reason": "revision_advanced_during_rebuild", "result": result},
            )
            db.commit()
            _emit_and_enqueue_project_task(
                db=db,
                task=task,
                request_id=str(params_dict.get("request_id") or "").strip() or None,
                event_type=None,
                source="worker",
                payload=None,
            )
            return task_id

        task.status = "succeeded"
        task.result_json = _compact_json_dumps(redact_api_keys(result))
        task.heartbeat_at = utc_now()
        task.finished_at = utc_now()
        append_project_task_event(
            db, task=task, event_type="succeeded", source="worker", payload={"result": redact_api_keys(result)}
        )
        db.commit()

        log_event(
            logger,
            "info",
            event="PROJECT_TASK_SUCCEEDED",
            task_id=task_id,
            project_id=str(task.project_id),
            kind=kind,
        )
        return task_id
    except Exception as exc:
        db.rollback()
        try:
            task2 = db.get(ProjectTask, task_id)
            if task2 is not None:
                safe_message = redact_secrets_text(str(exc)).replace("\n", " ").strip()
                if not safe_message:
                    safe_message = type(exc).__name__

                if isinstance(exc, AppError):
                    details = exc.details if isinstance(exc.details, dict) else {}
                    error_payload = {
                        "error_type": type(exc).__name__,
                        "code": str(exc.code),
                        "message": safe_message[:400],
                        "details": redact_api_keys(details),
                    }
                else:
                    error_payload = {"error_type": type(exc).__name__, "message": safe_message[:400]}

                task2.status = "failed"
                task2.error_json = _compact_json_dumps(error_payload)
                task2.heartbeat_at = utc_now()
                task2.finished_at = utc_now()
                append_project_task_event(
                    db,
                    task=task2,
                    event_type="failed",
                    source="worker",
                    payload={"error": redact_api_keys(error_payload)},
                )
                db.commit()
        except Exception:
            db.rollback()

        log_event(
            logger,
            "error",
            event="PROJECT_TASK_FAILED",
            task_id=task_id,
            error_type=type(exc).__name__,
            **exception_log_fields(exc),
        )
        return task_id
    finally:
        stop_project_task_heartbeat(locals().get("heartbeat_handle"))
        db.close()
