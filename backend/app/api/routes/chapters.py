from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Literal

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    DbDep,
    UserIdDep,
    require_chapter_editor,
    require_chapter_viewer,
    require_outline_viewer,
    require_project_editor,
    require_project_viewer,
)
from app.core.errors import AppError, ok_payload
from app.core.logging import exception_log_fields, log_event
from app.db.session import SessionLocal
from app.db.utils import new_id
from app.models.chapter import Chapter
from app.models.generation_run import GenerationRun
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_settings import ProjectSettings
from app.services.vector_index_state import mark_vector_index_dirty
from app.schemas.chapters import (
    BulkCreateRequest,
    ChapterCreate,
    ChapterDetailOut,
    ChapterListItemOut,
    ChapterMetaPageOut,
    ChapterOut,
    ChapterUpdate,
)
from app.schemas.chapter_generate import ChapterGenerateRequest
from app.schemas.chapter_plan import ChapterPlanRequest
from app.services.chapter_generation.app_service import (
    generate_chapter as generate_chapter_service,
    generate_chapter_precheck as generate_chapter_precheck_service,
    plan_chapter as plan_chapter_service,
)
from app.services.chapter_generation.stream_service import (
    generate_chapter_stream_events,
    prepare_chapter_stream_request,
)
from app.services.outline_store import ensure_active_outline
from app.services.project_task_service import schedule_chapter_done_tasks
from app.services.search_index_service import schedule_search_rebuild_task
from app.services.vector_rag_service import schedule_vector_rebuild_task
from app.utils.sse_response import create_sse_response

router = APIRouter()
logger = logging.getLogger("ainovel")

DEFAULT_CHAPTER_META_LIMIT = 200
MAX_CHAPTER_META_LIMIT = 500


class ChapterPostEditAdoption(BaseModel):
    generation_run_id: str = Field(max_length=36)
    post_edit_run_id: str | None = Field(default=None, max_length=36)
    choice: Literal["raw", "post_edit"]


class ChapterTriggerAutoUpdates(BaseModel):
    generation_run_id: str | None = Field(
        default=None,
        max_length=36,
        description="用于幂等的 token（优先使用 generation_run_id；不提供则回退 chapter.updated_at）",
    )


def _mark_vector_index_dirty(db: DbDep, *, project_id: str) -> None:
    mark_vector_index_dirty(db, project_id=project_id)


def _resolve_target_outline_id(*, db: Session, project_id: str, user_id: str, outline_id: str | None) -> str | None:
    project = require_project_viewer(db, project_id=project_id, user_id=user_id)
    if outline_id:
        outline = require_outline_viewer(db, outline_id=outline_id, user_id=user_id)
        if outline.project_id != project_id:
            raise AppError.validation("outline_id 不属于当前项目")
        return str(outline.id)
    if project.active_outline_id:
        return str(project.active_outline_id)
    return None


def _chapter_query(*, project_id: str, outline_id: str):
    return select(Chapter).where(Chapter.project_id == project_id, Chapter.outline_id == outline_id)


def _chapter_meta_payload(row: Chapter) -> dict:
    return ChapterListItemOut(
        id=str(row.id),
        project_id=str(row.project_id),
        outline_id=str(row.outline_id),
        number=int(row.number),
        title=row.title,
        status=str(row.status),
        updated_at=row.updated_at,
        has_plan=bool(str(row.plan or "").strip()),
        has_summary=bool(str(row.summary or "").strip()),
        has_content=bool(str(row.content_md or "").strip()),
    ).model_dump()


@router.get("/projects/{project_id}/chapters/meta")
def list_chapter_meta(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    outline_id: str | None = Query(default=None),
    cursor: int | None = Query(default=None, ge=0),
    limit: int = Query(default=DEFAULT_CHAPTER_META_LIMIT, ge=1, le=MAX_CHAPTER_META_LIMIT),
) -> dict:
    request_id = request.state.request_id
    target_outline_id = _resolve_target_outline_id(db=db, project_id=project_id, user_id=user_id, outline_id=outline_id)
    if target_outline_id is None:
        empty = ChapterMetaPageOut(chapters=[]).model_dump()
        return ok_payload(request_id=request_id, data=empty)

    filters = (Chapter.project_id == project_id, Chapter.outline_id == target_outline_id)
    total = int(db.execute(select(func.count(Chapter.id)).where(*filters)).scalar_one())

    query = select(Chapter).where(*filters)
    if cursor is not None:
        query = query.where(Chapter.number > cursor)
    rows = db.execute(query.order_by(Chapter.number.asc()).limit(limit + 1)).scalars().all()
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = page_rows[-1].number if has_more and page_rows else None
    data = ChapterMetaPageOut(
        chapters=[ChapterListItemOut.model_validate(_chapter_meta_payload(row)).model_dump() for row in page_rows],
        next_cursor=next_cursor,
        has_more=has_more,
        returned=len(page_rows),
        total=total,
    ).model_dump()
    return ok_payload(request_id=request_id, data=data)


@router.get("/projects/{project_id}/chapters")
def list_chapters(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    outline_id: str | None = Query(default=None),
) -> dict:
    request_id = request.state.request_id
    target_outline_id = _resolve_target_outline_id(db=db, project_id=project_id, user_id=user_id, outline_id=outline_id)
    if target_outline_id is None:
        return ok_payload(request_id=request_id, data={"chapters": []})

    rows = (
        db.execute(_chapter_query(project_id=project_id, outline_id=target_outline_id).order_by(Chapter.number.asc()))
        .scalars()
        .all()
    )
    return ok_payload(
        request_id=request_id, data={"chapters": [ChapterOut.model_validate(r).model_dump() for r in rows]}
    )


@router.post("/projects/{project_id}/chapters")
def create_chapter(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    body: ChapterCreate,
    outline_id: str | None = Query(default=None),
) -> dict:
    request_id = request.state.request_id
    project = require_project_editor(db, project_id=project_id, user_id=user_id)
    if outline_id:
        outline = require_outline_viewer(db, outline_id=outline_id, user_id=user_id)
        if outline.project_id != project_id:
            raise AppError.validation("outline_id 不属于当前项目")
        target_outline_id = outline.id
    else:
        target_outline_id = ensure_active_outline(db, project=project).id
    row = Chapter(
        id=new_id(),
        project_id=project_id,
        outline_id=target_outline_id,
        number=body.number,
        title=body.title,
        plan=body.plan,
        status=body.status,
    )
    db.add(row)
    try:
        _mark_vector_index_dirty(db, project_id=project_id)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError.conflict("章节号已存在", details={"field": "number"})
    db.refresh(row)
    if str(row.status or "") == "done":
        token = None
        updated_at = getattr(row, "updated_at", None)
        if updated_at is not None:
            token = updated_at.isoformat().replace("+00:00", "Z")
        try:
            schedule_chapter_done_tasks(
                db=db,
                project_id=project_id,
                actor_user_id=user_id,
                request_id=request_id,
                chapter_id=str(row.id),
                chapter_token=token,
                reason="chapter_done",
            )
        except Exception as exc:
            log_event(
                logger,
                "warning",
                event="CHAPTER_DONE_TASKS",
                action="trigger_failed",
                project_id=str(row.project_id),
                chapter_id=str(row.id),
                **exception_log_fields(exc),
            )
    else:
        schedule_vector_rebuild_task(
            db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="chapter_create"
        )
        schedule_search_rebuild_task(
            db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="chapter_create"
        )
    return ok_payload(request_id=request_id, data={"chapter": ChapterOut.model_validate(row).model_dump()})


@router.post("/projects/{project_id}/chapters/bulk_create")
def bulk_create(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    body: BulkCreateRequest,
    replace: bool = Query(default=False),
    outline_id: str | None = Query(default=None),
) -> dict:
    request_id = request.state.request_id
    project = require_project_editor(db, project_id=project_id, user_id=user_id)
    if outline_id:
        outline = require_outline_viewer(db, outline_id=outline_id, user_id=user_id)
        if outline.project_id != project_id:
            raise AppError.validation("outline_id 不属于当前项目")
        target_outline_id = outline.id
    else:
        target_outline_id = ensure_active_outline(db, project=project).id

    has_any = (
        db.execute(
            select(Chapter.id).where(Chapter.project_id == project_id, Chapter.outline_id == target_outline_id).limit(1)
        ).first()
        is not None
    )
    if has_any and not replace:
        raise AppError.conflict("该大纲已存在章节，无法创建（请选择覆盖创建）")

    numbers = [c.number for c in body.chapters]
    if len(numbers) != len(set(numbers)):
        raise AppError.validation("chapters.number 不能重复")

    try:
        if replace:
            db.execute(delete(Chapter).where(Chapter.project_id == project_id, Chapter.outline_id == target_outline_id))

        created: list[Chapter] = [
            Chapter(
                id=new_id(),
                project_id=project_id,
                outline_id=target_outline_id,
                number=c.number,
                title=c.title,
                plan=c.plan,
                status="planned",
            )
            for c in body.chapters
        ]
        db.add_all(created)
        _mark_vector_index_dirty(db, project_id=project_id)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AppError.conflict("章节创建冲突（请检查章节号）") from exc
    except Exception:
        db.rollback()
        raise

    created_sorted = sorted(created, key=lambda x: x.number)
    schedule_vector_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="chapters_bulk_create"
    )
    schedule_search_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="chapters_bulk_create"
    )
    return ok_payload(
        request_id=request_id,
        data={"chapters": [ChapterOut.model_validate(r).model_dump() for r in created_sorted]},
    )


@router.get("/chapters/{chapter_id}")
def get_chapter(request: Request, db: DbDep, user_id: UserIdDep, chapter_id: str) -> dict:
    request_id = request.state.request_id
    row = require_chapter_viewer(db, chapter_id=chapter_id, user_id=user_id)
    return ok_payload(request_id=request_id, data={"chapter": ChapterDetailOut.model_validate(row).model_dump()})


@router.put("/chapters/{chapter_id}")
def update_chapter(request: Request, db: DbDep, user_id: UserIdDep, chapter_id: str, body: ChapterUpdate) -> dict:
    request_id = request.state.request_id
    row = require_chapter_editor(db, chapter_id=chapter_id, user_id=user_id)
    prev_status = str(row.status or "")

    if body.title is not None:
        row.title = body.title
    if body.plan is not None:
        row.plan = body.plan
    if body.content_md is not None:
        row.content_md = body.content_md
    if body.summary is not None:
        row.summary = body.summary
    if body.status is not None:
        row.status = body.status

    _mark_vector_index_dirty(db, project_id=str(row.project_id))
    db.commit()
    db.refresh(row)

    next_status = str(row.status or "")
    if prev_status != "done" and next_status == "done":
        token = None
        updated_at = getattr(row, "updated_at", None)
        if updated_at is not None:
            token = updated_at.isoformat().replace("+00:00", "Z")

        try:
            schedule_chapter_done_tasks(
                db=db,
                project_id=str(row.project_id),
                actor_user_id=user_id,
                request_id=request_id,
                chapter_id=str(row.id),
                chapter_token=token,
                reason="chapter_done",
            )
        except Exception as exc:
            log_event(
                logger,
                "warning",
                event="CHAPTER_DONE_TASKS",
                action="trigger_failed",
                project_id=str(row.project_id),
                chapter_id=str(row.id),
                **exception_log_fields(exc),
            )
    else:
        schedule_vector_rebuild_task(
            db=db, project_id=str(row.project_id), actor_user_id=user_id, request_id=request_id, reason="chapter_update"
        )
        schedule_search_rebuild_task(
            db=db, project_id=str(row.project_id), actor_user_id=user_id, request_id=request_id, reason="chapter_update"
        )
    return ok_payload(request_id=request_id, data={"chapter": ChapterOut.model_validate(row).model_dump()})


@router.post("/chapters/{chapter_id}/trigger_auto_updates")
def trigger_chapter_auto_updates(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    chapter_id: str,
    body: ChapterTriggerAutoUpdates,
) -> dict:
    request_id = request.state.request_id
    chapter = require_chapter_editor(db, chapter_id=chapter_id, user_id=user_id)

    token: str | None = None
    run_id = str(body.generation_run_id or "").strip()
    if run_id:
        token = run_id
    else:
        updated_at = getattr(chapter, "updated_at", None)
        if updated_at is not None:
            token = updated_at.isoformat().replace("+00:00", "Z")

    tasks = schedule_chapter_done_tasks(
        db=db,
        project_id=str(chapter.project_id),
        actor_user_id=user_id,
        request_id=request_id,
        chapter_id=str(chapter.id),
        chapter_token=token,
        reason="chapter_auto_updates",
    )

    return ok_payload(request_id=request_id, data={"tasks": tasks, "chapter_token": token})


@router.post("/chapters/{chapter_id}/post_edit_adoption")
def record_post_edit_adoption(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    chapter_id: str,
    body: ChapterPostEditAdoption,
) -> dict:
    request_id = request.state.request_id
    chapter = require_chapter_editor(db, chapter_id=chapter_id, user_id=user_id)
    run = db.get(GenerationRun, str(body.generation_run_id))
    if not run:
        raise AppError.not_found("生成记录不存在")
    if str(run.project_id) != str(chapter.project_id) or str(run.chapter_id or "") != str(chapter_id):
        raise AppError.not_found("生成记录不存在")

    params: dict[str, object]
    if run.params_json:
        try:
            parsed = json.loads(run.params_json)
            params = parsed if isinstance(parsed, dict) else {"_raw": run.params_json}
        except Exception:
            params = {"_raw": run.params_json}
    else:
        params = {}

    params["post_edit_adoption"] = {
        "choice": body.choice,
        "post_edit_run_id": body.post_edit_run_id,
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    run.params_json = json.dumps(params, ensure_ascii=False)
    db.commit()

    return ok_payload(request_id=request_id, data={"ok": True})


@router.delete("/chapters/{chapter_id}")
def delete_chapter(request: Request, db: DbDep, user_id: UserIdDep, chapter_id: str) -> dict:
    request_id = request.state.request_id
    row = require_chapter_editor(db, chapter_id=chapter_id, user_id=user_id)
    db.delete(row)
    _mark_vector_index_dirty(db, project_id=str(row.project_id))
    db.commit()
    schedule_vector_rebuild_task(
        db=db, project_id=str(row.project_id), actor_user_id=user_id, request_id=request_id, reason="chapter_delete"
    )
    schedule_search_rebuild_task(
        db=db, project_id=str(row.project_id), actor_user_id=user_id, request_id=request_id, reason="chapter_delete"
    )
    return ok_payload(request_id=request_id, data={})


@router.post("/chapters/{chapter_id}/plan")
def plan_chapter(
    request: Request,
    chapter_id: str,
    body: ChapterPlanRequest,
    user_id: UserIdDep,
    x_llm_provider: str | None = Header(default=None, alias="X-LLM-Provider", max_length=64),
    x_llm_api_key: str | None = Header(default=None, alias="X-LLM-API-Key", max_length=4096),
) -> dict:
    request_id = request.state.request_id
    data = plan_chapter_service(
        logger=logger,
        request_id=request_id,
        chapter_id=chapter_id,
        body=body,
        user_id=user_id,
        x_llm_provider=x_llm_provider,
        x_llm_api_key=x_llm_api_key,
    )
    return ok_payload(request_id=request_id, data=data)


@router.post("/chapters/{chapter_id}/generate-precheck")
def generate_chapter_precheck(
    request: Request,
    chapter_id: str,
    body: ChapterGenerateRequest,
    user_id: UserIdDep,
    x_llm_provider: str | None = Header(default=None, alias="X-LLM-Provider", max_length=64),
    x_llm_api_key: str | None = Header(default=None, alias="X-LLM-API-Key", max_length=4096),
) -> dict:
    request_id = request.state.request_id
    return ok_payload(
        request_id=request_id,
        data=generate_chapter_precheck_service(
            logger=logger,
            request_id=request_id,
            chapter_id=chapter_id,
            body=body,
            user_id=user_id,
            x_llm_provider=x_llm_provider,
            x_llm_api_key=x_llm_api_key,
        ),
    )


@router.post("/chapters/{chapter_id}/generate")
def generate_chapter(
    request: Request,
    chapter_id: str,
    body: ChapterGenerateRequest,
    user_id: UserIdDep,
    x_llm_provider: str | None = Header(default=None, alias="X-LLM-Provider", max_length=64),
    x_llm_api_key: str | None = Header(default=None, alias="X-LLM-API-Key", max_length=4096),
) -> dict:
    request_id = request.state.request_id
    return ok_payload(
        request_id=request_id,
        data=generate_chapter_service(
            logger=logger,
            request_id=request_id,
            chapter_id=chapter_id,
            body=body,
            user_id=user_id,
            x_llm_provider=x_llm_provider,
            x_llm_api_key=x_llm_api_key,
        ),
    )


@router.post("/chapters/{chapter_id}/generate-stream")
def generate_chapter_stream(
    request: Request,
    chapter_id: str,
    body: ChapterGenerateRequest,
    user_id: UserIdDep,
    x_llm_provider: str | None = Header(default=None, alias="X-LLM-Provider", max_length=64),
    x_llm_api_key: str | None = Header(default=None, alias="X-LLM-API-Key", max_length=4096),
):
    request_id = request.state.request_id
    prepared = prepare_chapter_stream_request(
        logger=logger,
        request_id=request_id,
        chapter_id=chapter_id,
        body=body,
        user_id=user_id,
        x_llm_provider=x_llm_provider,
        x_llm_api_key=x_llm_api_key,
    )
    return create_sse_response(
        generate_chapter_stream_events(
            logger=logger,
            request_id=request_id,
            request_path=request.url.path,
            request_method=request.method,
            prepared=prepared,
            chapter_id=chapter_id,
            body=body,
            user_id=user_id,
        )
    )
