from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Header, Query, Request
from pydantic import Field
from sqlalchemy import func, or_, select

from app.api.deps import DbDep, UserIdDep, require_chapter_editor, require_project_editor, require_project_viewer
from app.core.errors import AppError, ok_payload
from app.db.session import SessionLocal
from app.db.utils import new_id, utc_now
from app.models.chapter import Chapter
from app.models.generation_run import GenerationRun
from app.models.memory_task import MemoryTask
from app.models.project import Project
from app.models.project_settings import ProjectSettings
from app.models.story_memory import StoryMemory
from app.models.structured_memory import (
    MemoryChangeSet,
    MemoryEntity,
    MemoryEvidence,
    MemoryEvent,
    MemoryForeshadow,
    MemoryRelation,
)
from app.models.user import User
from app.schemas.base import RequestModel
from app.schemas.memory_update import MemoryUpdateV1Request
from app.schemas.memory_preview import MemoryPreviewRequest
from app.services.generation_service import call_llm_and_record, with_param_overrides
from app.services.llm_task_preset_resolver import resolve_task_llm_config
from app.services.memory_retrieval_service import retrieve_memory_context_pack
from app.services.memory_update_service import (
    apply_memory_change_set,
    list_memory_change_sets,
    list_memory_tasks,
    memory_task_to_dict,
    propose_chapter_memory_change_set,
    propose_project_table_change_set,
    retry_memory_task,
    rollback_memory_change_set,
)
from app.services.table_executor import TableUpdateV1Request
from app.services.output_contracts import contract_for_task
from app.services.prompt_presets import _ensure_default_preset_from_resource, render_preset_for_task
from app.services.search_index_service import schedule_search_rebuild_task
from app.services.vector_rag_service import schedule_vector_rebuild_task

router = APIRouter()
logger = logging.getLogger("ainovel")


def _compact_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _resolve_task_llm_for_call(
    *,
    db,
    project: Project,
    user_id: str,
    task_key: str,
    x_llm_provider: str | None,
    x_llm_api_key: str | None,
):
    resolved = resolve_task_llm_config(
        db,
        project=project,
        user_id=user_id,
        task_key=task_key,
        header_api_key=x_llm_api_key,
    )
    if resolved is None:
        raise AppError(code="LLM_CONFIG_ERROR", message="请先在 Prompts 页保存 LLM 配置", status_code=400)
    if x_llm_api_key and x_llm_provider and resolved.llm_call.provider != x_llm_provider:
        raise AppError(code="LLM_CONFIG_ERROR", message="当前任务 provider 与请求头不一致，请先保存/切换", status_code=400)
    return resolved


def _require_chapter_done_for_memory_update(*, db: DbDep, chapter: Chapter, user_id: str, allow_draft: bool) -> None:
    status = str(getattr(chapter, "status", "") or "").strip().lower()
    if status == "done":
        return

    if allow_draft:
        actor = db.get(User, user_id)
        if actor is None or not bool(getattr(actor, "is_admin", False)):
            raise AppError.forbidden()
        return

    raise AppError.conflict(
        message="仅定稿章节可进行记忆更新",
        details={"reason": "chapter_not_done", "chapter_status": str(getattr(chapter, "status", "") or "")},
    )


@router.get("/projects/{project_id}/memory/retrieve")
def retrieve_project_memory(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    query_text: str = Query(default="", max_length=5000),
    include_deleted: bool = Query(default=False),
) -> dict:
    request_id = request.state.request_id
    require_project_viewer(db, project_id=project_id, user_id=user_id)
    pack = retrieve_memory_context_pack(db=db, project_id=project_id, query_text=query_text, include_deleted=include_deleted)
    return ok_payload(request_id=request_id, data=pack.model_dump())


@router.post("/projects/{project_id}/memory/preview")
def preview_project_memory(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    body: MemoryPreviewRequest,
) -> dict:
    request_id = request.state.request_id
    require_project_viewer(db, project_id=project_id, user_id=user_id)
    pack = retrieve_memory_context_pack(
        db=db,
        project_id=project_id,
        query_text=body.query_text,
        include_deleted=False,
        section_enabled=body.section_enabled,
        budget_overrides=body.budget_overrides,
    )
    return ok_payload(request_id=request_id, data=pack.model_dump())


StoryMemoryImportSchemaVersion = Literal["story_memory_import_v1"]


class StoryMemoryImportV1Item(RequestModel):
    chapter_id: str | None = Field(default=None, max_length=36)
    memory_type: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=255)
    content: str = Field(min_length=1, max_length=8000)
    importance_score: float = Field(default=0.0)
    story_timeline: int = Field(default=0)
    is_foreshadow: int = Field(default=0, ge=0, le=1)
    metadata: dict | None = None


class StoryMemoryImportV1Request(RequestModel):
    schema_version: StoryMemoryImportSchemaVersion = "story_memory_import_v1"
    source_tag: str | None = Field(default=None, max_length=64)
    replace_existing: bool = False
    memories: list[StoryMemoryImportV1Item] = Field(default_factory=list, max_length=2000)


@router.post("/projects/{project_id}/story_memories/import_all")
def import_all_story_memories(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    body: StoryMemoryImportV1Request,
) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)

    if str(body.schema_version or "").strip() != "story_memory_import_v1":
        raise AppError.validation(details={"reason": "unsupported_schema_version", "schema_version": body.schema_version})

    source_tag = str(body.source_tag or "").strip() or None
    if source_tag and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", source_tag):
        raise AppError.validation(message="source_tag 格式无效", details={"source_tag": source_tag})
    if body.replace_existing and not source_tag:
        raise AppError.validation(message="replace_existing=true 时必须提供 source_tag")

    created_ids: list[str] = []
    deleted_ids: list[str] = []
    now = utc_now()

    if body.replace_existing and source_tag:
        existing_rows = (
            db.execute(
                select(StoryMemory)
                .where(StoryMemory.project_id == project_id)
                .where(StoryMemory.metadata_json.is_not(None))
                .where(StoryMemory.metadata_json.like(f'%\"source\":\"{source_tag}\"%'))
            )
            .scalars()
            .all()
        )
        for row in existing_rows:
            deleted_ids.append(str(row.id))
            db.delete(row)

    for item in body.memories or []:
        chapter_id = str(item.chapter_id or "").strip() or None
        if chapter_id:
            chapter = db.get(Chapter, chapter_id)
            if chapter is None or str(getattr(chapter, "project_id", "")) != str(project_id):
                raise AppError.validation(details={"reason": "invalid_chapter_id", "chapter_id": chapter_id})

        title = str(item.title or "").strip() or None
        content = str(item.content or "").strip()
        if not content:
            continue
        metadata_obj = item.metadata if isinstance(item.metadata, dict) else {}
        if source_tag:
            metadata_obj = {"source": source_tag, **metadata_obj}
        elif not metadata_obj:
            metadata_obj = {"source": "import_all"}
        row = StoryMemory(
            id=new_id(),
            project_id=project_id,
            chapter_id=chapter_id,
            memory_type=str(item.memory_type or "").strip(),
            title=title,
            content=content,
            full_context_md=None,
            importance_score=float(item.importance_score or 0.0),
            tags_json=None,
            story_timeline=int(item.story_timeline or 0),
            text_position=-1,
            text_length=0,
            is_foreshadow=int(item.is_foreshadow or 0),
            foreshadow_resolved_at_chapter_id=None,
            metadata_json=_compact_json_dumps(metadata_obj),
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        created_ids.append(str(row.id))

    if not created_ids and not deleted_ids:
        if body.replace_existing and source_tag:
            return ok_payload(request_id=request_id, data={"created": 0, "ids": [], "deleted": 0, "deleted_ids": []})
        raise AppError.validation(message="未导入任何 story_memories", details={"reason": "empty"})

    settings_row = db.get(ProjectSettings, project_id)
    if settings_row is None:
        settings_row = ProjectSettings(project_id=project_id)
        db.add(settings_row)
    settings_row.vector_index_dirty = True

    db.commit()
    schedule_vector_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="story_memory_import_all"
    )
    schedule_search_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="story_memory_import_all"
    )
    return ok_payload(
        request_id=request_id,
        data={"created": len(created_ids), "ids": created_ids, "deleted": len(deleted_ids), "deleted_ids": deleted_ids},
    )


class StoryMemoryForeshadowResolveRequest(RequestModel):
    resolved_at_chapter_id: str | None = Field(default=None, max_length=64)


@router.get("/projects/{project_id}/story_memories/foreshadows/open_loops")
def list_story_memory_foreshadow_open_loops(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    q: str | None = Query(default=None, max_length=200),
    order: str = Query(default="timeline_desc", max_length=32),
) -> dict:
    request_id = request.state.request_id
    require_project_viewer(db, project_id=project_id, user_id=user_id)

    q_norm = str(q or "").strip()
    order_norm = str(order or "").strip().lower() or "timeline_desc"
    allowed_orders = {"timeline_desc", "importance_desc", "updated_desc"}
    if order_norm not in allowed_orders:
        raise AppError.validation(message="不支持的排序字段", details={"order": order_norm, "allowed": sorted(allowed_orders)})

    filters = [
        StoryMemory.project_id == project_id,
        StoryMemory.is_foreshadow == 1,  # noqa: E712
        StoryMemory.foreshadow_resolved_at_chapter_id.is_(None),
    ]
    if q_norm:
        pattern = f"%{q_norm}%"
        filters.append(or_(StoryMemory.title.ilike(pattern), StoryMemory.content.ilike(pattern)))

    if order_norm == "importance_desc":
        order_by = (StoryMemory.importance_score.desc(), StoryMemory.story_timeline.desc(), StoryMemory.updated_at.desc())
    elif order_norm == "updated_desc":
        order_by = (StoryMemory.updated_at.desc(), StoryMemory.story_timeline.desc(), StoryMemory.importance_score.desc())
    else:
        order_by = (StoryMemory.story_timeline.desc(), StoryMemory.importance_score.desc(), StoryMemory.updated_at.desc())

    rows = (
        db.execute(
            select(StoryMemory)
            .where(*filters)
            .order_by(*order_by)
            .limit(int(limit) + 1)
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > int(limit)
    rows = rows[: int(limit)]

    items = []
    for m in rows:
        content = str(m.content or "").strip()
        preview = (content[:200].rstrip() + "…") if len(content) > 200 else content
        items.append(
            {
                "id": m.id,
                "chapter_id": m.chapter_id,
                "memory_type": m.memory_type,
                "title": m.title,
                "importance_score": float(m.importance_score or 0.0),
                "story_timeline": int(m.story_timeline or 0),
                "is_foreshadow": bool(m.is_foreshadow),
                "resolved_at_chapter_id": m.foreshadow_resolved_at_chapter_id,
                "content_preview": preview,
                "updated_at": m.updated_at.isoformat() if m.updated_at else None,
            }
        )

    return ok_payload(request_id=request_id, data={"items": items, "has_more": bool(has_more), "returned": len(items)})


@router.post("/projects/{project_id}/story_memories/foreshadows/{story_memory_id}/resolve")
def resolve_story_memory_foreshadow(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    story_memory_id: str,
    body: StoryMemoryForeshadowResolveRequest,
) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)

    m = db.get(StoryMemory, story_memory_id)
    if m is None or str(m.project_id) != str(project_id):
        raise AppError.not_found()
    if not bool(getattr(m, "is_foreshadow", 0)):
        raise AppError.validation(message="该 StoryMemory 不是伏笔（foreshadow）", details={"story_memory_id": story_memory_id})

    resolved_at_chapter_id = str(body.resolved_at_chapter_id or "").strip() or None
    if resolved_at_chapter_id:
        chapter = db.get(Chapter, resolved_at_chapter_id)
        if chapter is None or str(getattr(chapter, "project_id", "")) != str(project_id):
            raise AppError.validation(
                message="回收章节（resolved_at_chapter_id）无效或不属于当前项目",
                details={"resolved_at_chapter_id": resolved_at_chapter_id},
            )

    m.foreshadow_resolved_at_chapter_id = resolved_at_chapter_id

    settings_row = db.get(ProjectSettings, project_id)
    if settings_row is None:
        settings_row = ProjectSettings(project_id=project_id)
        db.add(settings_row)
        db.flush()
    settings_row.vector_index_dirty = True

    db.commit()
    db.refresh(m)
    schedule_vector_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="story_memory_foreshadow_resolve"
    )
    schedule_search_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="story_memory_foreshadow_resolve"
    )

    return ok_payload(
        request_id=request_id,
        data={
            "foreshadow": {
                "id": m.id,
                "project_id": m.project_id,
                "chapter_id": m.chapter_id,
                "memory_type": m.memory_type,
                "title": m.title,
                "importance_score": float(m.importance_score or 0.0),
                "story_timeline": int(m.story_timeline or 0),
                "is_foreshadow": bool(m.is_foreshadow),
                "resolved_at_chapter_id": m.foreshadow_resolved_at_chapter_id,
                "updated_at": m.updated_at.isoformat() if m.updated_at else None,
            }
        },
    )


def _safe_json(raw: str | None, default: object) -> object:
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _parse_iso_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


class MemoryAutoProposeRequest(RequestModel):
    idempotency_key: str | None = Field(default=None, max_length=64)
    focus: str | None = Field(default=None, max_length=4000)


@router.get("/projects/{project_id}/memory/structured")
def list_structured_memory(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    include_deleted: bool = Query(default=False),
    table: str | None = Query(default=None, max_length=32),
    q: str | None = Query(default=None, max_length=200),
    before: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    request_id = request.state.request_id
    require_project_viewer(db, project_id=project_id, user_id=user_id)

    table_norm = str(table or "").strip().lower() or None
    allowed_tables = {"entities", "relations", "events", "foreshadows", "evidence"}
    if table_norm is not None and table_norm not in allowed_tables:
        raise AppError.validation(details={"reason": "invalid_table", "table": table})

    keyword = str(q or "").strip() or None
    pattern = f"%{keyword}%" if keyword else None

    before_dt: datetime | None = None
    if table_norm is not None and before is not None:
        before_dt = _parse_iso_dt(before)
        if before_dt is None:
            raise AppError.validation(details={"reason": "invalid_before", "before": before})

    def _count(table_name: str) -> int:
        if table_name == "entities":
            cond = [MemoryEntity.project_id == project_id]
            if not include_deleted:
                cond.append(MemoryEntity.deleted_at.is_(None))
            if pattern:
                cond.append(or_(MemoryEntity.name.like(pattern), MemoryEntity.summary_md.like(pattern), MemoryEntity.entity_type.like(pattern)))
            return int(db.execute(select(func.count()).select_from(MemoryEntity).where(*cond)).scalar_one())
        if table_name == "relations":
            cond = [MemoryRelation.project_id == project_id]
            if not include_deleted:
                cond.append(MemoryRelation.deleted_at.is_(None))
            if pattern:
                cond.append(or_(MemoryRelation.relation_type.like(pattern), MemoryRelation.description_md.like(pattern)))
            return int(db.execute(select(func.count()).select_from(MemoryRelation).where(*cond)).scalar_one())
        if table_name == "events":
            cond = [MemoryEvent.project_id == project_id]
            if not include_deleted:
                cond.append(MemoryEvent.deleted_at.is_(None))
            if pattern:
                cond.append(or_(MemoryEvent.title.like(pattern), MemoryEvent.content_md.like(pattern), MemoryEvent.event_type.like(pattern)))
            return int(db.execute(select(func.count()).select_from(MemoryEvent).where(*cond)).scalar_one())
        if table_name == "foreshadows":
            cond = [MemoryForeshadow.project_id == project_id]
            if not include_deleted:
                cond.append(MemoryForeshadow.deleted_at.is_(None))
            if pattern:
                cond.append(or_(MemoryForeshadow.title.like(pattern), MemoryForeshadow.content_md.like(pattern)))
            return int(db.execute(select(func.count()).select_from(MemoryForeshadow).where(*cond)).scalar_one())
        if table_name == "evidence":
            cond = [MemoryEvidence.project_id == project_id]
            if not include_deleted:
                cond.append(MemoryEvidence.deleted_at.is_(None))
            if pattern:
                cond.append(or_(MemoryEvidence.quote_md.like(pattern), MemoryEvidence.source_type.like(pattern), MemoryEvidence.source_id.like(pattern)))
            return int(db.execute(select(func.count()).select_from(MemoryEvidence).where(*cond)).scalar_one())
        raise AppError.validation(details={"reason": "invalid_table", "table": table_name})

    counts = {name: _count(name) for name in sorted(allowed_tables)}

    data: dict[str, object] = {"counts": counts, "cursor": {}, "table": table_norm, "q": keyword}

    cursors: dict[str, str | None] = {name: None for name in allowed_tables}

    if table_norm in (None, "entities"):
        cond = [MemoryEntity.project_id == project_id]
        if not include_deleted:
            cond.append(MemoryEntity.deleted_at.is_(None))
        if pattern:
            cond.append(or_(MemoryEntity.name.like(pattern), MemoryEntity.summary_md.like(pattern), MemoryEntity.entity_type.like(pattern)))
        q_entities = select(MemoryEntity).where(*cond)
        if table_norm == "entities" and before_dt is not None:
            q_entities = q_entities.where(MemoryEntity.updated_at < before_dt)
        rows = db.execute(q_entities.order_by(MemoryEntity.updated_at.desc(), MemoryEntity.id.desc()).limit(limit + 1)).scalars().all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        cursors["entities"] = rows[-1].updated_at.isoformat() if (has_more and rows) else None
        data["entities"] = [
            {
                "id": e.id,
                "project_id": e.project_id,
                "entity_type": e.entity_type,
                "name": e.name,
                "summary_md": e.summary_md,
                "attributes": _safe_json(e.attributes_json, {}),
                "deleted_at": e.deleted_at.isoformat() if e.deleted_at else None,
                "created_at": e.created_at.isoformat(),
                "updated_at": e.updated_at.isoformat(),
            }
            for e in rows
        ]
    else:
        data["entities"] = []

    if table_norm in (None, "relations"):
        cond = [MemoryRelation.project_id == project_id]
        if not include_deleted:
            cond.append(MemoryRelation.deleted_at.is_(None))
        if pattern:
            cond.append(or_(MemoryRelation.relation_type.like(pattern), MemoryRelation.description_md.like(pattern)))
        q_relations = select(MemoryRelation).where(*cond)
        if table_norm == "relations" and before_dt is not None:
            q_relations = q_relations.where(MemoryRelation.updated_at < before_dt)
        rows = (
            db.execute(q_relations.order_by(MemoryRelation.updated_at.desc(), MemoryRelation.id.desc()).limit(limit + 1))
            .scalars()
            .all()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        cursors["relations"] = rows[-1].updated_at.isoformat() if (has_more and rows) else None
        data["relations"] = [
            {
                "id": r.id,
                "project_id": r.project_id,
                "from_entity_id": r.from_entity_id,
                "to_entity_id": r.to_entity_id,
                "relation_type": r.relation_type,
                "description_md": r.description_md,
                "attributes": _safe_json(r.attributes_json, {}),
                "deleted_at": r.deleted_at.isoformat() if r.deleted_at else None,
                "created_at": r.created_at.isoformat(),
                "updated_at": r.updated_at.isoformat(),
            }
            for r in rows
        ]
    else:
        data["relations"] = []

    if table_norm in (None, "events"):
        cond = [MemoryEvent.project_id == project_id]
        if not include_deleted:
            cond.append(MemoryEvent.deleted_at.is_(None))
        if pattern:
            cond.append(or_(MemoryEvent.title.like(pattern), MemoryEvent.content_md.like(pattern), MemoryEvent.event_type.like(pattern)))
        q_events = select(MemoryEvent).where(*cond)
        if table_norm == "events" and before_dt is not None:
            q_events = q_events.where(MemoryEvent.updated_at < before_dt)
        rows = db.execute(q_events.order_by(MemoryEvent.updated_at.desc(), MemoryEvent.id.desc()).limit(limit + 1)).scalars().all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        cursors["events"] = rows[-1].updated_at.isoformat() if (has_more and rows) else None
        data["events"] = [
            {
                "id": ev.id,
                "project_id": ev.project_id,
                "chapter_id": ev.chapter_id,
                "event_type": ev.event_type,
                "title": ev.title,
                "content_md": ev.content_md,
                "attributes": _safe_json(ev.attributes_json, {}),
                "deleted_at": ev.deleted_at.isoformat() if ev.deleted_at else None,
                "created_at": ev.created_at.isoformat(),
                "updated_at": ev.updated_at.isoformat(),
            }
            for ev in rows
        ]
    else:
        data["events"] = []

    if table_norm in (None, "foreshadows"):
        cond = [MemoryForeshadow.project_id == project_id]
        if not include_deleted:
            cond.append(MemoryForeshadow.deleted_at.is_(None))
        if pattern:
            cond.append(or_(MemoryForeshadow.title.like(pattern), MemoryForeshadow.content_md.like(pattern)))
        q_foreshadows = select(MemoryForeshadow).where(*cond)
        if table_norm == "foreshadows" and before_dt is not None:
            q_foreshadows = q_foreshadows.where(MemoryForeshadow.updated_at < before_dt)
        rows = (
            db.execute(q_foreshadows.order_by(MemoryForeshadow.updated_at.desc(), MemoryForeshadow.id.desc()).limit(limit + 1))
            .scalars()
            .all()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        cursors["foreshadows"] = rows[-1].updated_at.isoformat() if (has_more and rows) else None
        data["foreshadows"] = [
            {
                "id": f.id,
                "project_id": f.project_id,
                "chapter_id": f.chapter_id,
                "resolved_at_chapter_id": f.resolved_at_chapter_id,
                "title": f.title,
                "content_md": f.content_md,
                "resolved": f.resolved,
                "attributes": _safe_json(f.attributes_json, {}),
                "deleted_at": f.deleted_at.isoformat() if f.deleted_at else None,
                "created_at": f.created_at.isoformat(),
                "updated_at": f.updated_at.isoformat(),
            }
            for f in rows
        ]
    else:
        data["foreshadows"] = []

    if table_norm in (None, "evidence"):
        cond = [MemoryEvidence.project_id == project_id]
        if not include_deleted:
            cond.append(MemoryEvidence.deleted_at.is_(None))
        if pattern:
            cond.append(or_(MemoryEvidence.quote_md.like(pattern), MemoryEvidence.source_type.like(pattern), MemoryEvidence.source_id.like(pattern)))
        q_evidence = select(MemoryEvidence).where(*cond)
        if table_norm == "evidence" and before_dt is not None:
            q_evidence = q_evidence.where(MemoryEvidence.created_at < before_dt)
        rows = (
            db.execute(q_evidence.order_by(MemoryEvidence.created_at.desc(), MemoryEvidence.id.desc()).limit(limit + 1)).scalars().all()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        cursors["evidence"] = rows[-1].created_at.isoformat() if (has_more and rows) else None
        data["evidence"] = [
            {
                "id": ev.id,
                "project_id": ev.project_id,
                "source_type": ev.source_type,
                "source_id": ev.source_id,
                "quote_md": ev.quote_md,
                "attributes": _safe_json(ev.attributes_json, {}),
                "deleted_at": ev.deleted_at.isoformat() if ev.deleted_at else None,
                "created_at": ev.created_at.isoformat(),
            }
            for ev in rows
        ]
    else:
        data["evidence"] = []

    data["cursor"] = cursors
    return ok_payload(request_id=request_id, data=data)


@router.post("/chapters/{chapter_id}/memory/propose")
def propose_chapter_memory_update(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    chapter_id: str,
    body: MemoryUpdateV1Request,
    allow_draft: bool = Query(default=False),
) -> dict:
    request_id = request.state.request_id
    chapter = require_chapter_editor(db, chapter_id=chapter_id, user_id=user_id)
    _require_chapter_done_for_memory_update(db=db, chapter=chapter, user_id=user_id, allow_draft=allow_draft)
    out = propose_chapter_memory_change_set(db=db, request_id=request_id, actor_user_id=user_id, chapter=chapter, payload=body)
    return ok_payload(request_id=request_id, data=out)


@router.post("/projects/{project_id}/tables/change_sets/propose")
def propose_project_table_update(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    body: TableUpdateV1Request,
) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)
    out = propose_project_table_change_set(
        db=db,
        request_id=request_id,
        actor_user_id=user_id,
        project_id=project_id,
        payload=body,
    )
    return ok_payload(request_id=request_id, data=out)


@router.post("/chapters/{chapter_id}/memory/propose/auto")
def auto_propose_chapter_memory_update(
    request: Request,
    chapter_id: str,
    body: MemoryAutoProposeRequest,
    user_id: UserIdDep,
    allow_draft: bool = Query(default=False),
    x_llm_provider: str | None = Header(default=None, alias="X-LLM-Provider", max_length=64),
    x_llm_api_key: str | None = Header(default=None, alias="X-LLM-API-Key", max_length=4096),
) -> dict:
    request_id = request.state.request_id
    resolved_api_key = ""

    prompt_system = ""
    prompt_user = ""
    prompt_render_log_json: str | None = None
    prompt_messages = None
    llm_call = None
    project_id = ""

    focus = (body.focus or "").strip()
    idempotency_key = (body.idempotency_key or "").strip() or f"memupd-auto-{new_id()[:8]}"

    db = SessionLocal()
    try:
        chapter = require_chapter_editor(db, chapter_id=chapter_id, user_id=user_id)
        _require_chapter_done_for_memory_update(db=db, chapter=chapter, user_id=user_id, allow_draft=allow_draft)
        project_id = str(chapter.project_id)
        project = db.get(Project, project_id)
        if project is None:
            raise AppError.not_found()

        resolved_memupd = _resolve_task_llm_for_call(
            db=db,
            project=project,
            user_id=user_id,
            task_key="memory_update",
            x_llm_provider=x_llm_provider,
            x_llm_api_key=x_llm_api_key,
        )
        resolved_api_key = str(resolved_memupd.api_key)

        _ensure_default_preset_from_resource(db, project_id=project_id, resource_key="memory_update_v1", activate=True)
        values = {
            "chapter_id": str(chapter.id),
            "chapter_number": int(chapter.number),
            "chapter_title": str(chapter.title or ""),
            "chapter_plan": str(chapter.plan or ""),
            "chapter_content_md": str(chapter.content_md or ""),
            "focus": focus,
        }

        prompt_system, prompt_user, prompt_messages, _, _, _, render_log = render_preset_for_task(
            db,
            project_id=project_id,
            task="memory_update",
            values=values,
            macro_seed=f"{request_id}:memory_update",
            provider=resolved_memupd.llm_call.provider,
        )
        prompt_render_log_json = json.dumps(render_log, ensure_ascii=False)
        llm_call = resolved_memupd.llm_call
    finally:
        db.close()

    if llm_call is None:
        raise AppError(code="INTERNAL_ERROR", message="LLM 调用准备失败", status_code=500)
    if not prompt_system.strip() and not prompt_user.strip():
        raise AppError(code="PROMPT_CONFIG_ERROR", message="缺少 memory_update 提示词预设/提示块", status_code=400)

    llm_call = with_param_overrides(llm_call, {"temperature": 0.2, "max_tokens": 2048})
    llm_result = call_llm_and_record(
        logger=logger,
        request_id=request_id,
        actor_user_id=user_id,
        project_id=project_id,
        chapter_id=chapter_id,
        run_type="memory_update_auto_propose",
        api_key=str(resolved_api_key),
        prompt_system=prompt_system,
        prompt_user=prompt_user,
        prompt_messages=prompt_messages,
        prompt_render_log_json=prompt_render_log_json,
        llm_call=llm_call,
    )

    contract = contract_for_task("memory_update")
    parsed = contract.parse(llm_result.text, finish_reason=llm_result.finish_reason)
    if parsed.parse_error is not None:
        raise AppError.validation(
            message="记忆更新（memory_update）输出不符合 JSON 契约",
            details={
                "parse_error": parsed.parse_error,
                "warnings": parsed.warnings,
                "generation_run_id": llm_result.run_id,
                "finish_reason": llm_result.finish_reason,
            },
        )

    payload = MemoryUpdateV1Request(
        schema_version="memory_update_v1",
        idempotency_key=idempotency_key,
        title=str(parsed.data.get("title") or "Memory Update (auto)").strip() or "Memory Update (auto)",
        summary_md=str(parsed.data.get("summary_md") or "").strip() or None,
        ops=list(parsed.data.get("ops") or []),
    )

    db2 = SessionLocal()
    try:
        chapter2 = require_chapter_editor(db2, chapter_id=chapter_id, user_id=user_id)
        _require_chapter_done_for_memory_update(db=db2, chapter=chapter2, user_id=user_id, allow_draft=allow_draft)
        out = propose_chapter_memory_change_set(
            db=db2,
            request_id=request_id,
            actor_user_id=user_id,
            chapter=chapter2,
            payload=payload,
        )
        out["llm_generation_run_id"] = llm_result.run_id
    finally:
        db2.close()
    return ok_payload(request_id=request_id, data=out)


@router.post("/memory_change_sets/{change_set_id}/apply")
def apply_memory_update(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    change_set_id: str,
    allow_draft: bool = Query(default=False),
) -> dict:
    request_id = request.state.request_id
    change_set = db.get(MemoryChangeSet, change_set_id)
    if change_set is None:
        raise AppError.not_found()
    require_project_editor(db, project_id=str(change_set.project_id), user_id=user_id)

    run = db.get(GenerationRun, str(change_set.generation_run_id)) if change_set.generation_run_id else None
    chapter_id = str(getattr(run, "chapter_id", "") or "").strip()
    if chapter_id:
        chapter = db.get(Chapter, chapter_id)
        if chapter is not None:
            _require_chapter_done_for_memory_update(db=db, chapter=chapter, user_id=user_id, allow_draft=allow_draft)

    out = apply_memory_change_set(db=db, request_id=request_id, actor_user_id=user_id, change_set=change_set)
    return ok_payload(request_id=request_id, data=out)


@router.get("/projects/{project_id}/memory_change_sets")
def list_project_memory_change_sets(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    status: str | None = Query(default=None, max_length=16),
    before: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    request_id = request.state.request_id
    require_project_viewer(db, project_id=project_id, user_id=user_id)
    out = list_memory_change_sets(db=db, project_id=project_id, status=status, before=before, limit=limit)
    return ok_payload(request_id=request_id, data=out)


@router.get("/projects/{project_id}/memory_tasks")
def list_project_memory_tasks(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    status: str | None = Query(default=None, max_length=16),
    before: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    request_id = request.state.request_id
    require_project_viewer(db, project_id=project_id, user_id=user_id)
    out = list_memory_tasks(db=db, project_id=project_id, status=status, before=before, limit=limit)
    return ok_payload(request_id=request_id, data=out)


@router.get("/memory_tasks/{task_id}")
def get_memory_task(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    task_id: str,
) -> dict:
    request_id = request.state.request_id
    task = db.get(MemoryTask, task_id)
    if task is None:
        raise AppError.not_found()
    require_project_viewer(db, project_id=str(task.project_id), user_id=user_id)
    change_set = db.get(MemoryChangeSet, str(task.change_set_id))
    return ok_payload(request_id=request_id, data=memory_task_to_dict(task=task, change_set_request_id=change_set.request_id if change_set else None))


@router.post("/memory_tasks/{task_id}/retry")
def retry_memory_task_endpoint(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    task_id: str,
) -> dict:
    request_id = request.state.request_id
    task = db.get(MemoryTask, task_id)
    if task is None:
        raise AppError.not_found()
    require_project_editor(db, project_id=str(task.project_id), user_id=user_id)

    retry_memory_task(db=db, request_id=request_id, task=task)
    change_set = db.get(MemoryChangeSet, str(task.change_set_id))
    return ok_payload(
        request_id=request_id,
        data=memory_task_to_dict(task=task, change_set_request_id=change_set.request_id if change_set else None),
    )


@router.post("/memory_change_sets/{change_set_id}/rollback")
def rollback_memory_update(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    change_set_id: str,
) -> dict:
    request_id = request.state.request_id
    change_set = db.get(MemoryChangeSet, change_set_id)
    if change_set is None:
        raise AppError.not_found()
    require_project_editor(db, project_id=str(change_set.project_id), user_id=user_id)
    out = rollback_memory_change_set(db=db, request_id=request_id, actor_user_id=user_id, change_set=change_set)
    return ok_payload(request_id=request_id, data=out)
