from __future__ import annotations

"""Vector rebuild scheduling plus compatibility re-exports.

Storage backend implementation and mutable state live exclusively in
``vector_storage``.  The re-exports below preserve existing import paths while
keeping every callable identical to its owning implementation.
"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import exception_log_fields, log_event
from app.db.session import SessionLocal
from app.db.utils import new_id, utc_now
from app.models.project_settings import ProjectSettings
from app.models.project_task import ProjectTask
from app.services.project_task_event_service import emit_and_enqueue_project_task, reset_project_task_to_queued
from app.services.vector_storage import (
    _INMEMORY_CHROMA,
    _INMEMORY_CHROMADB,
    _PGVECTOR_READY_CACHE_TTL_SECONDS,
    _PGVECTOR_TABLE,
    _InMemoryChromaModule,
    _InMemoryClient,
    _InMemoryCollection,
    _backend_dir,
    _chroma_collection_naming,
    _cosine_distance,
    _default_chroma_persist_dir,
    _get_collection,
    _hash_collection_name,
    _import_chromadb,
    _is_postgres,
    _legacy_collection_name,
    _migrate_chroma_collection,
    _normalize_kb_id,
    _pgvector_delete_project,
    _pgvector_hybrid_fetch,
    _pgvector_hybrid_query,
    _pgvector_literal,
    _pgvector_ready,
    _pgvector_upsert_chunks,
    _prefer_pgvector,
    _rrf_contrib,
    _rrf_score,
    _safe_json_loads,
    _vector_enabled_reason,
    ingest_chunks,
    purge_project_vectors,
    rebuild_project,
)
from app.services.vector_types import VectorChunk, VectorSource, _ALL_SOURCES

logger = logging.getLogger("ainovel")


def schedule_vector_rebuild_task(
    *,
    db: Session | None = None,
    project_id: str,
    actor_user_id: str | None,
    request_id: str | None,
    reason: str,
) -> str | None:
    """
    Fail-soft scheduler: ensure/enqueue a ProjectTask(kind=vector_rebuild) for the project.

    Idempotency remains tied to the last successful build to coalesce a burst
    of mutations into one database task. ``vector_dirty_revision`` is captured
    in the task parameters so the worker can fence completion and requeue the
    same task when content changes during a rebuild.
    """

    pid = str(project_id or "").strip()
    if not pid:
        return None
    reason_norm = str(reason or "").strip() or "dirty"
    owns_session = db is None
    if db is None:
        db = SessionLocal()
    try:
        settings_row = db.get(ProjectSettings, pid)
        if settings_row is not None and not bool(getattr(settings_row, "vector_index_dirty", False)):
            return None

        dirty_revision = int(getattr(settings_row, "vector_dirty_revision", 0) or 0)
        last_build_at = getattr(settings_row, "last_vector_build_at", None) if settings_row is not None else None
        token = "none"
        if last_build_at is not None:
            token = last_build_at.isoformat().replace("+00:00", "Z")

        idempotency_key = f"vector:project:since:{token}:v1"
        task = (
            db.execute(
                select(ProjectTask).where(
                    ProjectTask.project_id == pid,
                    ProjectTask.idempotency_key == idempotency_key,
                )
            )
            .scalars()
            .first()
        )

        created_task = False
        if task is None:
            created_task = True
            task = ProjectTask(
                id=new_id(),
                project_id=pid,
                actor_user_id=actor_user_id,
                kind="vector_rebuild",
                status="queued",
                idempotency_key=idempotency_key,
                params_json=json.dumps(
                    {
                        "reason": reason_norm,
                        "request_id": request_id,
                        "dirty_revision": dirty_revision,
                        "triggered_at": utc_now().isoformat().replace("+00:00", "Z"),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                result_json=None,
                error_json=None,
            )
            db.add(task)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                task = (
                    db.execute(
                        select(ProjectTask).where(
                            ProjectTask.project_id == pid,
                            ProjectTask.idempotency_key == idempotency_key,
                        )
                    )
                    .scalars()
                    .first()
                )
                if task is None:
                    return None
        else:
            status_norm = str(getattr(task, "status", "") or "").strip().lower()
            event_type = None
            if status_norm not in {"queued", "running"}:
                reset_project_task_to_queued(task=task, increment_retry_count=status_norm == "failed")
                db.commit()
                event_type = "retry" if status_norm == "failed" else "queued"
            else:
                event_type = None
        return emit_and_enqueue_project_task(
            db,
            task=task,
            request_id=request_id,
            logger=logger,
            event_type=("queued" if created_task else event_type),
            source="scheduler",
            payload={"reason": reason_norm, "request_id": request_id},
        )
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        log_event(
            logger,
            "warning",
            event="VECTOR_REBUILD_SCHEDULE_ERROR",
            project_id=pid,
            error_type=type(exc).__name__,
            request_id=request_id,
            **exception_log_fields(exc),
        )
        return None
    finally:
        if owns_session:
            db.close()


__all__ = [
    "VectorChunk",
    "VectorSource",
    "_ALL_SOURCES",
    "_PGVECTOR_TABLE",
    "_PGVECTOR_READY_CACHE_TTL_SECONDS",
    "schedule_vector_rebuild_task",
    "ingest_chunks",
    "rebuild_project",
    "purge_project_vectors",
]
