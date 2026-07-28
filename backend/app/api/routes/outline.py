from __future__ import annotations

import json

from fastapi import APIRouter, Header, Request
from sqlalchemy import select

from app.api.deps import DbDep, UserIdDep, require_project_editor, require_project_viewer
from app.core.errors import ok_payload
from app.db.utils import utc_now
from app.models.outline import Outline
from app.models.project_settings import ProjectSettings
from app.schemas.outline import OutlineOut, OutlineUpdate
from app.schemas.outline_generate import OutlineGenerateRequest
from app.services.outline_generation.app_service import (
    generate_outline as generate_outline_service,
    generate_outline_stream_events,
    prepare_outline_stream_request,
)
from app.services.outline_payload_normalizer import (
    normalize_outline_content_and_structure,
    parse_outline_structure_json,
)
from app.services.outline_store import ensure_active_outline
from app.services.search_index_service import schedule_search_rebuild_task
from app.services.vector_rag_service import schedule_vector_rebuild_task
from app.services.vector_index_state import mark_vector_index_dirty
from app.utils.sse_response import create_sse_response

router = APIRouter()


def _default_outline(project_id: str) -> Outline:
    """Transient default payload for outline-less projects — never persisted.

    id="" 表示"尚未落库"；首次 PUT /outline（或建章）经 ensure_active_outline
    物化同样内容（backend-api#5：GET 必须零副作用）。
    """
    now = utc_now()
    return Outline(
        id="",
        project_id=project_id,
        title="默认大纲",
        content_md="",
        structure_json=None,
        created_at=now,
        updated_at=now,
    )


def _outline_out(row: Outline) -> dict[str, object]:
    parsed_structure = parse_outline_structure_json(row.structure_json)
    content_md, structure, _ = normalize_outline_content_and_structure(
        content_md=row.content_md or "", structure=parsed_structure
    )
    return OutlineOut(
        id=row.id,
        project_id=row.project_id,
        title=row.title,
        content_md=content_md,
        structure=structure,
        created_at=row.created_at,
        updated_at=row.updated_at,
    ).model_dump()


def _mark_vector_index_dirty(db: DbDep, *, project_id: str) -> None:
    mark_vector_index_dirty(db, project_id=project_id)


@router.get("/projects/{project_id}/outline")
def get_outline(request: Request, db: DbDep, user_id: UserIdDep, project_id: str) -> dict:
    request_id = request.state.request_id
    project = require_project_viewer(db, project_id=project_id, user_id=user_id)
    row = db.get(Outline, project.active_outline_id) if project.active_outline_id else None
    if row is None:
        row = (
            db.execute(
                select(Outline).where(Outline.project_id == project_id).order_by(Outline.updated_at.desc()).limit(1)
            )
            .scalars()
            .first()
        )
    if row is None:
        row = _default_outline(project_id)
    return ok_payload(request_id=request_id, data={"outline": _outline_out(row)})


@router.put("/projects/{project_id}/outline")
def put_outline(request: Request, db: DbDep, user_id: UserIdDep, project_id: str, body: OutlineUpdate) -> dict:
    request_id = request.state.request_id
    project = require_project_editor(db, project_id=project_id, user_id=user_id)
    row = ensure_active_outline(db, project=project)

    if body.title is not None:
        row.title = body.title

    if body.content_md is not None:
        content_md, structure, normalized = normalize_outline_content_and_structure(
            content_md=body.content_md,
            structure=body.structure,
        )
        row.content_md = content_md
        if body.structure is not None or normalized:
            row.structure_json = json.dumps(structure, ensure_ascii=False) if structure is not None else None
    elif body.structure is not None:
        row.structure_json = json.dumps(body.structure, ensure_ascii=False)

    _mark_vector_index_dirty(db, project_id=project_id)
    db.commit()
    db.refresh(row)
    schedule_vector_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="outline_update"
    )
    schedule_search_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="outline_update"
    )
    return ok_payload(request_id=request_id, data={"outline": _outline_out(row)})


@router.post("/projects/{project_id}/outline/generate")
def generate_outline(
    request: Request,
    project_id: str,
    body: OutlineGenerateRequest,
    user_id: UserIdDep,
    x_llm_provider: str | None = Header(default=None, alias="X-LLM-Provider", max_length=64),
    x_llm_api_key: str | None = Header(default=None, alias="X-LLM-API-Key", max_length=4096),
) -> dict:
    request_id = request.state.request_id
    data = generate_outline_service(
        request_id=request_id,
        project_id=project_id,
        body=body,
        user_id=user_id,
        x_llm_provider=x_llm_provider,
        x_llm_api_key=x_llm_api_key,
    )
    return ok_payload(request_id=request_id, data=data)


@router.post("/projects/{project_id}/outline/generate-stream")
def generate_outline_stream(
    request: Request,
    project_id: str,
    body: OutlineGenerateRequest,
    user_id: UserIdDep,
    x_llm_provider: str | None = Header(default=None, alias="X-LLM-Provider", max_length=64),
    x_llm_api_key: str | None = Header(default=None, alias="X-LLM-API-Key", max_length=4096),
):
    request_id = request.state.request_id
    prepared = prepare_outline_stream_request(
        project_id=project_id,
        body=body,
        user_id=user_id,
        request_id=request_id,
        x_llm_provider=x_llm_provider,
        x_llm_api_key=x_llm_api_key,
    )
    return create_sse_response(
        generate_outline_stream_events(
            request_id=request_id,
            project_id=project_id,
            body=body,
            user_id=user_id,
            prepared=prepared,
        )
    )
