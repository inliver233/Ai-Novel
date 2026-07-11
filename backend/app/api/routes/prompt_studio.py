from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import DbDep, UserIdDep, require_project_editor, require_project_viewer
from app.core.errors import ok_payload
from app.schemas.prompt_studio import PromptStudioPresetCreate, PromptStudioPresetUpdate
from app.services.prompt_studio_service import (
    activate_preset_payload,
    create_preset_payload,
    delete_preset_payload,
    get_preset_detail_payload,
    list_categories_payload,
    update_preset_payload,
)

router = APIRouter()


@router.get("/projects/{project_id}/prompt-studio/categories")
def list_prompt_studio_categories(request: Request, db: DbDep, user_id: UserIdDep, project_id: str) -> dict:
    request_id = request.state.request_id
    require_project_viewer(db, project_id=project_id, user_id=user_id)
    return ok_payload(request_id=request_id, data=list_categories_payload(db, project_id=project_id, user_id=user_id))


@router.get("/projects/{project_id}/prompt-studio/presets/{preset_id}")
def get_prompt_studio_preset(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    preset_id: str,
    category: str,
) -> dict:
    request_id = request.state.request_id
    require_project_viewer(db, project_id=project_id, user_id=user_id)
    return ok_payload(
        request_id=request_id,
        data=get_preset_detail_payload(
            db,
            project_id=project_id,
            user_id=user_id,
            preset_id=preset_id,
            category=category,
        ),
    )


@router.post("/projects/{project_id}/prompt-studio/presets")
def create_prompt_studio_preset(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    body: PromptStudioPresetCreate,
    category: str,
) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)
    return ok_payload(
        request_id=request_id,
        data=create_preset_payload(
            db,
            project_id=project_id,
            user_id=user_id,
            category=category,
            body=body,
        ),
    )


@router.put("/projects/{project_id}/prompt-studio/presets/{preset_id}")
def update_prompt_studio_preset(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    preset_id: str,
    body: PromptStudioPresetUpdate,
) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)
    return ok_payload(
        request_id=request_id,
        data=update_preset_payload(
            db,
            project_id=project_id,
            user_id=user_id,
            preset_id=preset_id,
            body=body,
        ),
    )


@router.delete("/projects/{project_id}/prompt-studio/presets/{preset_id}")
def delete_prompt_studio_preset(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    preset_id: str,
) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)
    return ok_payload(
        request_id=request_id,
        data=delete_preset_payload(
            db,
            project_id=project_id,
            user_id=user_id,
            preset_id=preset_id,
        ),
    )


@router.put("/projects/{project_id}/prompt-studio/presets/{preset_id}/activate")
def activate_prompt_studio_preset(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    preset_id: str,
    category: str,
) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)
    return ok_payload(
        request_id=request_id,
        data=activate_preset_payload(
            db,
            project_id=project_id,
            user_id=user_id,
            preset_id=preset_id,
            category=category,
        ),
    )
