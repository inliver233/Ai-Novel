from __future__ import annotations

import json

from fastapi import APIRouter, Request
from sqlalchemy import delete, func, select

from app.api.deps import (
    DbDep,
    UserIdDep,
    require_outline_editor,
    require_outline_viewer,
    require_project_editor,
    require_project_viewer,
)
from app.core.errors import AppError, ok_payload
from app.db.utils import new_id
from app.models.chapter import Chapter
from app.models.outline import Outline
from app.models.project_settings import ProjectSettings
from app.schemas.outline import OutlineCreate, OutlineListItem, OutlineOut, OutlineUpdate
from app.services.outline_payload_normalizer import (
    normalize_outline_content_and_structure,
    parse_outline_structure_json,
)
from app.services.search_index_service import schedule_search_rebuild_task
from app.services.vector_rag_service import schedule_vector_rebuild_task
from app.services.vector_index_state import mark_vector_index_dirty

router = APIRouter()


def _mark_vector_index_dirty(db: DbDep, *, project_id: str) -> None:
    mark_vector_index_dirty(db, project_id=project_id)


def _outline_out(row: Outline) -> dict:
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


@router.get("/projects/{project_id}/outlines")
def list_outlines(request: Request, db: DbDep, user_id: UserIdDep, project_id: str) -> dict:
    request_id = request.state.request_id
    require_project_viewer(db, project_id=project_id, user_id=user_id)

    rows = (
        db.execute(select(Outline).where(Outline.project_id == project_id).order_by(Outline.updated_at.desc()))
        .scalars()
        .all()
    )
    counts = dict(
        db.execute(
            select(Chapter.outline_id, func.count(Chapter.id))
            .where(Chapter.project_id == project_id)
            .group_by(Chapter.outline_id)
        ).all()
    )
    items = [
        OutlineListItem(
            id=r.id,
            title=r.title,
            created_at=r.created_at,
            updated_at=r.updated_at,
            has_chapters=bool(counts.get(r.id, 0)),
        ).model_dump()
        for r in rows
    ]
    return ok_payload(request_id=request_id, data={"outlines": items})


@router.post("/projects/{project_id}/outlines")
def create_outline(request: Request, db: DbDep, user_id: UserIdDep, project_id: str, body: OutlineCreate) -> dict:
    request_id = request.state.request_id
    project = require_project_editor(db, project_id=project_id, user_id=user_id)
    content_md, structure, _ = normalize_outline_content_and_structure(
        content_md=body.content_md, structure=body.structure
    )

    row = Outline(
        id=new_id(),
        project_id=project_id,
        title=body.title,
        content_md=content_md or "",
        structure_json=json.dumps(structure, ensure_ascii=False) if structure is not None else None,
    )
    db.add(row)
    project.active_outline_id = row.id
    _mark_vector_index_dirty(db, project_id=project_id)
    db.commit()
    db.refresh(row)
    schedule_vector_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="outline_create"
    )
    schedule_search_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="outline_create"
    )
    return ok_payload(request_id=request_id, data={"outline": _outline_out(row)})


@router.get("/projects/{project_id}/outlines/{outline_id}")
def get_outline_item(request: Request, db: DbDep, user_id: UserIdDep, project_id: str, outline_id: str) -> dict:
    request_id = request.state.request_id
    row = require_outline_viewer(db, outline_id=outline_id, user_id=user_id)
    if row.project_id != project_id:
        raise AppError.not_found()
    return ok_payload(request_id=request_id, data={"outline": _outline_out(row)})


@router.put("/projects/{project_id}/outlines/{outline_id}")
def update_outline_item(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    outline_id: str,
    body: OutlineUpdate,
) -> dict:
    request_id = request.state.request_id
    row = require_outline_editor(db, outline_id=outline_id, user_id=user_id)
    if row.project_id != project_id:
        raise AppError.not_found()

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


@router.delete("/projects/{project_id}/outlines/{outline_id}")
def delete_outline_item(request: Request, db: DbDep, user_id: UserIdDep, project_id: str, outline_id: str) -> dict:
    request_id = request.state.request_id
    project = require_project_editor(db, project_id=project_id, user_id=user_id)
    row = require_outline_editor(db, outline_id=outline_id, user_id=user_id)
    if row.project_id != project_id:
        raise AppError.not_found()

    db.execute(delete(Chapter).where(Chapter.outline_id == outline_id))
    db.delete(row)

    if project.active_outline_id == outline_id:
        next_outline = (
            db.execute(
                select(Outline)
                .where(Outline.project_id == project_id, Outline.id != outline_id)
                .order_by(Outline.updated_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        project.active_outline_id = next_outline.id if next_outline else None

    _mark_vector_index_dirty(db, project_id=project_id)
    db.commit()
    schedule_vector_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="outline_delete"
    )
    schedule_search_rebuild_task(
        db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="outline_delete"
    )
    return ok_payload(request_id=request_id, data={})
