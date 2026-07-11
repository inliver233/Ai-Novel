from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.api.deps import DbDep, UserIdDep, require_project_editor, require_project_viewer
from app.core.errors import AppError, ok_payload
from app.db.utils import new_id, utc_now
from app.models.chapter import Chapter
from app.models.project_settings import ProjectSettings
from app.models.story_memory import StoryMemory
from app.schemas.story_memory import (
    StoryMemoryCreateRequest,
    StoryMemoryMarkDoneRequest,
    StoryMemoryMergeRequest,
    StoryMemoryUpdateRequest,
)
from app.services.search_index_service import schedule_search_rebuild_task
from app.services.vector_rag_service import schedule_vector_rebuild_task
from app.services.vector_index_state import mark_vector_index_dirty

router = APIRouter()


def _parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _parse_json_obj(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _compact_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _mark_vector_index_dirty(db: DbDep, *, project_id: str) -> None:
    mark_vector_index_dirty(db, project_id=project_id)


def _tags_to_json(tags: list[str] | None) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for raw in tags or []:
        t = str(raw or "").strip()
        if not t:
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
        if len(out) >= 80:
            break
    return _compact_json_dumps(out) if out else "[]"


def _is_done(m: StoryMemory) -> bool:
    meta = _parse_json_obj(getattr(m, "metadata_json", None))
    return bool(meta.get("done")) if isinstance(meta, dict) else False


def _to_out(m: StoryMemory) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "project_id": str(m.project_id),
        "chapter_id": str(m.chapter_id) if m.chapter_id else None,
        "memory_type": str(m.memory_type or ""),
        "title": str(m.title) if m.title is not None else None,
        "content": str(m.content or ""),
        "full_context_md": str(m.full_context_md) if m.full_context_md is not None else None,
        "importance_score": float(m.importance_score or 0.0),
        "tags": _parse_json_list(getattr(m, "tags_json", None)),
        "story_timeline": int(m.story_timeline or 0),
        "text_position": int(m.text_position or -1),
        "text_length": int(m.text_length or 0),
        "is_foreshadow": bool(getattr(m, "is_foreshadow", 0)),
        "resolved_at_chapter_id": str(m.foreshadow_resolved_at_chapter_id)
        if m.foreshadow_resolved_at_chapter_id
        else None,
        "done": _is_done(m),
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


@router.get("/projects/{project_id}/story_memories")
def list_story_memories(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    chapter_id: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    request_id = request.state.request_id
    require_project_viewer(db, project_id=project_id, user_id=user_id)

    filters = [StoryMemory.project_id == project_id]
    if chapter_id is not None and str(chapter_id).strip():
        filters.append(StoryMemory.chapter_id == str(chapter_id))

    rows = (
        db.execute(
            select(StoryMemory)
            .where(*filters)
            .order_by(StoryMemory.updated_at.desc(), StoryMemory.id.desc())
            .limit(limit + 1)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_to_out(m) for m in rows]
    next_offset = (offset + limit) if has_more else None
    return ok_payload(request_id=request_id, data={"items": items, "next_offset": next_offset})


@router.post("/projects/{project_id}/story_memories")
def create_story_memory(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    body: StoryMemoryCreateRequest,
) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)

    chapter_id = str(body.chapter_id or "").strip() or None
    if chapter_id:
        chapter = db.get(Chapter, chapter_id)
        if chapter is None or str(getattr(chapter, "project_id", "")) != str(project_id):
            raise AppError.validation(details={"reason": "invalid_chapter_id", "chapter_id": chapter_id})

    row = StoryMemory(
        id=new_id(),
        project_id=project_id,
        chapter_id=chapter_id,
        memory_type=str(body.memory_type).strip(),
        title=str(body.title).strip() if isinstance(body.title, str) and body.title.strip() else None,
        content=str(body.content or ""),
        full_context_md=str(body.full_context_md or "").strip() or None,
        importance_score=float(body.importance_score or 0.0),
        tags_json=_tags_to_json(list(body.tags or [])),
        story_timeline=int(body.story_timeline or 0),
        text_position=int(body.text_position or -1),
        text_length=int(body.text_length or 0),
        is_foreshadow=1 if bool(body.is_foreshadow) else 0,
    )
    db.add(row)
    _mark_vector_index_dirty(db, project_id=project_id)
    db.commit()
    db.refresh(row)

    schedule_vector_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="story_memory_create"
    )
    schedule_search_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="story_memory_create"
    )

    return ok_payload(request_id=request_id, data={"story_memory": _to_out(row)})


@router.put("/projects/{project_id}/story_memories/{story_memory_id}")
def update_story_memory(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    story_memory_id: str,
    body: StoryMemoryUpdateRequest,
) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)

    row = db.get(StoryMemory, story_memory_id)
    if row is None or str(getattr(row, "project_id", "")) != str(project_id):
        raise AppError.not_found()

    if body.chapter_id is not None:
        chapter_id = str(body.chapter_id or "").strip() or None
        if chapter_id:
            chapter = db.get(Chapter, chapter_id)
            if chapter is None or str(getattr(chapter, "project_id", "")) != str(project_id):
                raise AppError.validation(details={"reason": "invalid_chapter_id", "chapter_id": chapter_id})
        row.chapter_id = chapter_id

    if body.memory_type is not None and str(body.memory_type or "").strip():
        row.memory_type = str(body.memory_type).strip()
    if body.title is not None:
        row.title = str(body.title).strip() if isinstance(body.title, str) and body.title.strip() else None
    if body.content is not None:
        row.content = str(body.content or "")
    if body.full_context_md is not None:
        row.full_context_md = str(body.full_context_md or "").strip() or None
    if body.importance_score is not None:
        row.importance_score = float(body.importance_score or 0.0)
    if body.tags is not None:
        row.tags_json = _tags_to_json(list(body.tags or []))
    if body.story_timeline is not None:
        row.story_timeline = int(body.story_timeline or 0)
    if body.text_position is not None:
        row.text_position = int(body.text_position)
    if body.text_length is not None:
        row.text_length = int(body.text_length or 0)
    if body.is_foreshadow is not None:
        row.is_foreshadow = 1 if bool(body.is_foreshadow) else 0
        if not bool(body.is_foreshadow):
            row.foreshadow_resolved_at_chapter_id = None

    _mark_vector_index_dirty(db, project_id=project_id)
    db.commit()
    db.refresh(row)

    schedule_vector_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="story_memory_refresh"
    )
    schedule_search_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="story_memory_refresh"
    )

    return ok_payload(request_id=request_id, data={"story_memory": _to_out(row)})


@router.delete("/projects/{project_id}/story_memories/{story_memory_id}")
def delete_story_memory(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    story_memory_id: str,
) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)

    row = db.get(StoryMemory, story_memory_id)
    if row is None or str(getattr(row, "project_id", "")) != str(project_id):
        raise AppError.not_found()

    db.delete(row)
    _mark_vector_index_dirty(db, project_id=project_id)
    db.commit()

    schedule_vector_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="story_memory_delete"
    )
    schedule_search_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="story_memory_delete"
    )
    return ok_payload(request_id=request_id, data={"deleted_id": str(story_memory_id)})


@router.post("/projects/{project_id}/story_memories/merge")
def merge_story_memories(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    body: StoryMemoryMergeRequest,
) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)

    target_id = str(body.target_id or "").strip()
    if not target_id:
        raise AppError.validation(details={"reason": "target_id_empty"})

    source_ids = [str(x or "").strip() for x in (body.source_ids or []) if str(x or "").strip()]
    source_ids = [x for x in source_ids if x != target_id]
    if not source_ids:
        raise AppError.validation(details={"reason": "source_ids_empty"})

    target = db.get(StoryMemory, target_id)
    if target is None or str(getattr(target, "project_id", "")) != str(project_id):
        raise AppError.not_found()

    sources = (
        db.execute(select(StoryMemory).where(StoryMemory.project_id == project_id, StoryMemory.id.in_(source_ids)))
        .scalars()
        .all()
    )
    if len(sources) != len(set(source_ids)):
        found = {str(s.id) for s in sources}
        missing = [sid for sid in source_ids if sid not in found]
        raise AppError.not_found("部分 story_memories 不存在", details={"missing_ids": missing})

    target_tags = _parse_json_list(getattr(target, "tags_json", None))
    merged_tags = list(target_tags)
    merged_content = str(target.content or "")
    merged_full_context = str(target.full_context_md or "").strip()
    merged_importance = float(target.importance_score or 0.0)
    merged_is_foreshadow = bool(getattr(target, "is_foreshadow", 0))

    deleted_ids: list[str] = []
    for src in sources:
        merged_content = (
            (merged_content + "\n\n---\n\n" + str(src.content or "")).strip()
            if merged_content.strip()
            else str(src.content or "")
        )
        if not merged_full_context.strip():
            merged_full_context = str(src.full_context_md or "").strip()
        merged_tags.extend(_parse_json_list(getattr(src, "tags_json", None)))
        merged_importance = max(merged_importance, float(src.importance_score or 0.0))
        merged_is_foreshadow = merged_is_foreshadow or bool(getattr(src, "is_foreshadow", 0))
        if bool(getattr(src, "is_foreshadow", 0)) and getattr(src, "foreshadow_resolved_at_chapter_id", None):
            target.foreshadow_resolved_at_chapter_id = src.foreshadow_resolved_at_chapter_id
        db.delete(src)
        deleted_ids.append(str(src.id))

    target.content = merged_content
    target.full_context_md = merged_full_context.strip() or None
    target.tags_json = _tags_to_json(merged_tags)
    target.importance_score = merged_importance
    target.is_foreshadow = 1 if merged_is_foreshadow else 0

    _mark_vector_index_dirty(db, project_id=project_id)
    db.commit()
    db.refresh(target)

    schedule_vector_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="story_memory_merge"
    )
    schedule_search_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="story_memory_merge"
    )

    return ok_payload(request_id=request_id, data={"story_memory": _to_out(target), "deleted_ids": deleted_ids})


@router.post("/projects/{project_id}/story_memories/{story_memory_id}/mark_done")
def mark_story_memory_done(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    story_memory_id: str,
    body: StoryMemoryMarkDoneRequest,
) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)

    row = db.get(StoryMemory, story_memory_id)
    if row is None or str(getattr(row, "project_id", "")) != str(project_id):
        raise AppError.not_found()

    meta = _parse_json_obj(getattr(row, "metadata_json", None))
    done = bool(getattr(body, "done", True))
    if done:
        meta["done"] = True
        meta["done_at"] = utc_now().isoformat().replace("+00:00", "Z")
    else:
        meta.pop("done", None)
        meta.pop("done_at", None)
    row.metadata_json = _compact_json_dumps(meta) if meta else None

    _mark_vector_index_dirty(db, project_id=project_id)
    db.commit()
    db.refresh(row)

    schedule_vector_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="story_memory_mark_done"
    )
    schedule_search_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="story_memory_mark_done"
    )

    return ok_payload(request_id=request_id, data={"story_memory": _to_out(row)})
