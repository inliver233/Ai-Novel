from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.utils import new_id, utc_now
from app.models.project_settings import ProjectSettings
from app.models.story_memory import StoryMemory
from app.services.search_index_service import schedule_search_rebuild_task
from app.services.vector_rag_service import schedule_vector_rebuild_task
from app.services.vector_index_state import mark_vector_index_dirty

__all__ = ["import_story_memories"]


def _validate_story_memory_import_schema_version(schema_version: str | None) -> None:
    if str(schema_version or "").strip() != "story_memory_import_v1":
        raise AppError.validation(details={"reason": "unsupported_schema_version", "schema_version": schema_version})


def _ensure_story_memory_rebuild_dirty(db: Session, *, project_id: str, flush_on_create: bool = False) -> None:
    del flush_on_create
    mark_vector_index_dirty(db, project_id=project_id)


def _build_story_memory_import_row(*, project_id: str, item: Any, now: datetime) -> StoryMemory | None:
    title = str(getattr(item, "title", None) or "").strip() or None
    content = str(getattr(item, "content", "") or "").strip()
    if not content:
        return None
    return StoryMemory(
        id=new_id(),
        project_id=project_id,
        chapter_id=None,
        memory_type=str(getattr(item, "memory_type", "") or "").strip(),
        title=title,
        content=content,
        full_context_md=None,
        importance_score=float(getattr(item, "importance_score", 0.0) or 0.0),
        tags_json=None,
        story_timeline=int(getattr(item, "story_timeline", 0) or 0),
        text_position=-1,
        text_length=0,
        metadata_json=json.dumps({"source": "import_all"}, ensure_ascii=False),
        created_at=now,
        updated_at=now,
    )


def import_story_memories(
    db: Session,
    *,
    project_id: str,
    schema_version: str | None,
    items: list[object],
    actor_user_id: str,
    request_id: str,
) -> dict[str, object]:
    _validate_story_memory_import_schema_version(schema_version)
    now = utc_now()
    rows = []
    for item in items:
        row = _build_story_memory_import_row(project_id=project_id, item=item, now=now)
        if row is not None:
            rows.append(row)
            db.add(row)

    created_ids = [str(row.id) for row in rows]
    if not created_ids:
        raise AppError.validation(message="未导入任何 story_memories", details={"reason": "empty"})

    _ensure_story_memory_rebuild_dirty(db, project_id=project_id)
    db.commit()
    schedule_vector_rebuild_task(
        db=db,
        project_id=project_id,
        actor_user_id=actor_user_id,
        request_id=request_id,
        reason="story_memory_import_all",
    )
    schedule_search_rebuild_task(
        db=db,
        project_id=project_id,
        actor_user_id=actor_user_id,
        request_id=request_id,
        reason="story_memory_import_all",
    )
    return {"created": len(created_ids), "ids": created_ids}
