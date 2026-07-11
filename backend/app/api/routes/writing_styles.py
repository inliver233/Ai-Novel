from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select, update

from app.api.deps import DbDep, UserIdDep, require_project_editor, require_project_viewer
from app.core.errors import AppError, ok_payload
from app.db.utils import new_id
from app.models.project_default_style import ProjectDefaultStyle
from app.models.writing_style import WritingStyle
from app.schemas.writing_styles import (
    ProjectDefaultStyleOut,
    ProjectDefaultStylePutRequest,
    WritingStyleCreateRequest,
    WritingStyleOut,
    WritingStyleUpdateRequest,
)

router = APIRouter()


def _to_out(row: WritingStyle) -> dict:
    return WritingStyleOut(
        id=row.id,
        owner_user_id=row.owner_user_id,
        name=row.name,
        description=row.description,
        prompt_content=row.prompt_content,
        is_preset=bool(row.is_preset),
        created_at=row.created_at,
        updated_at=row.updated_at,
    ).model_dump()


def _require_owned_user_style(db: DbDep, *, style_id: str, user_id: str) -> WritingStyle:
    row = db.get(WritingStyle, style_id)
    if row is None or row.is_preset or row.owner_user_id != user_id:
        raise AppError.not_found()
    return row


@router.get("/writing_styles/presets")
def list_presets(request: Request, db: DbDep, user_id: UserIdDep) -> dict:
    request_id = request.state.request_id
    rows = (
        db.execute(select(WritingStyle).where(WritingStyle.is_preset == True).order_by(WritingStyle.name.asc()))
        .scalars()
        .all()
    )
    return ok_payload(request_id=request_id, data={"styles": [_to_out(r) for r in rows]})


@router.get("/writing_styles")
def list_user_styles(request: Request, db: DbDep, user_id: UserIdDep) -> dict:
    request_id = request.state.request_id
    rows = (
        db.execute(
            select(WritingStyle)
            .where(WritingStyle.owner_user_id == user_id)
            .where(WritingStyle.is_preset == False)
            .order_by(WritingStyle.updated_at.desc())
        )
        .scalars()
        .all()
    )
    return ok_payload(request_id=request_id, data={"styles": [_to_out(r) for r in rows]})


@router.post("/writing_styles")
def create_style(request: Request, db: DbDep, user_id: UserIdDep, body: WritingStyleCreateRequest) -> dict:
    request_id = request.state.request_id
    row = WritingStyle(
        id=new_id(),
        owner_user_id=user_id,
        name=body.name,
        description=body.description,
        prompt_content=body.prompt_content,
        is_preset=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return ok_payload(request_id=request_id, data={"style": _to_out(row)})


@router.put("/writing_styles/{style_id}")
def update_style(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    style_id: str,
    body: WritingStyleUpdateRequest,
) -> dict:
    request_id = request.state.request_id
    row = _require_owned_user_style(db, style_id=style_id, user_id=user_id)

    if body.name is not None:
        row.name = body.name
    if "description" in body.model_fields_set:
        row.description = body.description
    if body.prompt_content is not None:
        row.prompt_content = body.prompt_content

    db.commit()
    db.refresh(row)
    return ok_payload(request_id=request_id, data={"style": _to_out(row)})


@router.delete("/writing_styles/{style_id}")
def delete_style(request: Request, db: DbDep, user_id: UserIdDep, style_id: str) -> dict:
    request_id = request.state.request_id
    row = _require_owned_user_style(db, style_id=style_id, user_id=user_id)

    db.execute(update(ProjectDefaultStyle).where(ProjectDefaultStyle.style_id == style_id).values(style_id=None))
    db.delete(row)
    db.commit()
    return ok_payload(request_id=request_id, data={})


@router.get("/projects/{project_id}/writing_style_default")
def get_project_default_style(request: Request, db: DbDep, user_id: UserIdDep, project_id: str) -> dict:
    request_id = request.state.request_id
    require_project_viewer(db, project_id=project_id, user_id=user_id)
    row = db.get(ProjectDefaultStyle, project_id)
    style_id = row.style_id if row else None
    out = ProjectDefaultStyleOut(project_id=project_id, style_id=style_id, updated_at=row.updated_at if row else None)
    return ok_payload(request_id=request_id, data={"default": out.model_dump()})


@router.put("/projects/{project_id}/writing_style_default")
def put_project_default_style(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    body: ProjectDefaultStylePutRequest,
) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)

    style_id = body.style_id
    if style_id is not None:
        style = db.get(WritingStyle, style_id)
        if style is None:
            raise AppError.validation(message="风格（style_id）不存在")
        if not style.is_preset and style.owner_user_id != user_id:
            raise AppError.forbidden(message="无权限使用该风格")

    row = db.get(ProjectDefaultStyle, project_id)
    if row is None:
        row = ProjectDefaultStyle(project_id=project_id, style_id=style_id)
        db.add(row)
    else:
        row.style_id = style_id

    db.commit()
    db.refresh(row)
    out = ProjectDefaultStyleOut(project_id=project_id, style_id=row.style_id, updated_at=row.updated_at)
    return ok_payload(request_id=request_id, data={"default": out.model_dump()})
