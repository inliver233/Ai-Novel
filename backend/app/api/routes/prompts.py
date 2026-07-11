from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import DbDep, UserIdDep, require_project_editor
from app.services.prompt_management.app import (
    build_prompt_import_all_payload,
    build_prompt_preset_detail_payload,
    build_prompt_preset_export_payload,
    build_prompt_preset_list_payload,
    build_prompt_preset_resources_payload,
    build_prompt_presets_export_all_payload,
    build_prompt_preview_response,
    create_prompt_block_payload,
    create_prompt_preset_payload,
    delete_prompt_block_payload,
    delete_prompt_preset_payload,
    import_prompt_preset_payload,
    reorder_prompt_blocks_payload,
    require_prompt_block_with_preset,
    require_prompt_preset,
    reset_prompt_block_payload,
    reset_prompt_preset_payload,
    update_prompt_block_payload,
    update_prompt_preset_payload,
)
from app.core.errors import ok_payload
from app.schemas.prompt_presets import (
    PromptBlockCreate,
    PromptBlockReorderRequest,
    PromptBlockUpdate,
    PromptPresetCreate,
    PromptPresetImportAllRequest,
    PromptPresetImportRequest,
    PromptPresetUpdate,
    PromptPreviewRequest,
)

router = APIRouter()


@router.get("/projects/{project_id}/prompt_presets")
def list_prompt_presets(request: Request, db: DbDep, user_id: UserIdDep, project_id: str) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)
    return ok_payload(request_id=request_id, data=build_prompt_preset_list_payload(db, project_id=project_id))


@router.get("/projects/{project_id}/prompt_preset_resources")
def list_prompt_preset_resources(request: Request, db: DbDep, user_id: UserIdDep, project_id: str) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)
    return ok_payload(request_id=request_id, data=build_prompt_preset_resources_payload(db, project_id=project_id))


@router.post("/projects/{project_id}/prompt_presets")
def create_prompt_preset(request: Request, db: DbDep, user_id: UserIdDep, project_id: str, body: PromptPresetCreate) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)
    return ok_payload(request_id=request_id, data=create_prompt_preset_payload(db, project_id=project_id, body=body))


@router.get("/prompt_presets/{preset_id}")
def get_prompt_preset(request: Request, db: DbDep, user_id: UserIdDep, preset_id: str) -> dict:
    request_id = request.state.request_id
    preset = require_prompt_preset(db, preset_id=preset_id)
    require_project_editor(db, project_id=preset.project_id, user_id=user_id)
    return ok_payload(request_id=request_id, data=build_prompt_preset_detail_payload(db, preset=preset))


@router.put("/prompt_presets/{preset_id}")
def update_prompt_preset(request: Request, db: DbDep, user_id: UserIdDep, preset_id: str, body: PromptPresetUpdate) -> dict:
    request_id = request.state.request_id
    preset = require_prompt_preset(db, preset_id=preset_id)
    require_project_editor(db, project_id=preset.project_id, user_id=user_id)
    return ok_payload(request_id=request_id, data=update_prompt_preset_payload(db, preset=preset, body=body))


@router.post("/prompt_presets/{preset_id}/reset_to_default")
def reset_prompt_preset_to_default(request: Request, db: DbDep, user_id: UserIdDep, preset_id: str) -> dict:
    request_id = request.state.request_id
    preset = require_prompt_preset(db, preset_id=preset_id)
    require_project_editor(db, project_id=preset.project_id, user_id=user_id)
    return ok_payload(request_id=request_id, data=reset_prompt_preset_payload(db, preset=preset))


@router.delete("/prompt_presets/{preset_id}")
def delete_prompt_preset(request: Request, db: DbDep, user_id: UserIdDep, preset_id: str) -> dict:
    request_id = request.state.request_id
    preset = require_prompt_preset(db, preset_id=preset_id)
    require_project_editor(db, project_id=preset.project_id, user_id=user_id)
    return ok_payload(request_id=request_id, data=delete_prompt_preset_payload(db, preset=preset))


@router.post("/prompt_presets/{preset_id}/blocks")
def create_prompt_block(request: Request, db: DbDep, user_id: UserIdDep, preset_id: str, body: PromptBlockCreate) -> dict:
    request_id = request.state.request_id
    preset = require_prompt_preset(db, preset_id=preset_id)
    require_project_editor(db, project_id=preset.project_id, user_id=user_id)
    return ok_payload(request_id=request_id, data=create_prompt_block_payload(db, preset=preset, body=body))


@router.put("/prompt_blocks/{block_id}")
def update_prompt_block(request: Request, db: DbDep, user_id: UserIdDep, block_id: str, body: PromptBlockUpdate) -> dict:
    request_id = request.state.request_id
    block, preset = require_prompt_block_with_preset(db, block_id=block_id)
    require_project_editor(db, project_id=preset.project_id, user_id=user_id)
    return ok_payload(request_id=request_id, data=update_prompt_block_payload(db, preset=preset, block=block, body=body))


@router.post("/prompt_blocks/{block_id}/reset_to_default")
def reset_prompt_block_to_default(request: Request, db: DbDep, user_id: UserIdDep, block_id: str) -> dict:
    request_id = request.state.request_id
    block, preset = require_prompt_block_with_preset(db, block_id=block_id)
    require_project_editor(db, project_id=preset.project_id, user_id=user_id)
    return ok_payload(request_id=request_id, data=reset_prompt_block_payload(db, preset=preset, block=block))


@router.delete("/prompt_blocks/{block_id}")
def delete_prompt_block(request: Request, db: DbDep, user_id: UserIdDep, block_id: str) -> dict:
    request_id = request.state.request_id
    block, preset = require_prompt_block_with_preset(db, block_id=block_id)
    require_project_editor(db, project_id=preset.project_id, user_id=user_id)
    return ok_payload(request_id=request_id, data=delete_prompt_block_payload(db, preset=preset, block=block))


@router.post("/prompt_presets/{preset_id}/blocks/reorder")
def reorder_prompt_blocks(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    preset_id: str,
    body: PromptBlockReorderRequest,
) -> dict:
    request_id = request.state.request_id
    preset = require_prompt_preset(db, preset_id=preset_id)
    require_project_editor(db, project_id=preset.project_id, user_id=user_id)
    return ok_payload(
        request_id=request_id,
        data=reorder_prompt_blocks_payload(db, preset=preset, ordered_block_ids=list(body.ordered_block_ids or [])),
    )


@router.get("/prompt_presets/{preset_id}/export")
def export_prompt_preset(request: Request, db: DbDep, user_id: UserIdDep, preset_id: str) -> dict:
    request_id = request.state.request_id
    preset = require_prompt_preset(db, preset_id=preset_id)
    require_project_editor(db, project_id=preset.project_id, user_id=user_id)
    return ok_payload(request_id=request_id, data=build_prompt_preset_export_payload(db, preset=preset))


@router.post("/projects/{project_id}/prompt_presets/import")
def import_prompt_preset(request: Request, db: DbDep, user_id: UserIdDep, project_id: str, body: PromptPresetImportRequest) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)
    return ok_payload(request_id=request_id, data=import_prompt_preset_payload(db, project_id=project_id, body=body))


@router.get("/projects/{project_id}/prompt_presets/export_all")
def export_all_prompt_presets(request: Request, db: DbDep, user_id: UserIdDep, project_id: str) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)
    return ok_payload(request_id=request_id, data=build_prompt_presets_export_all_payload(db, project_id=project_id))


@router.post("/projects/{project_id}/prompt_presets/import_all")
def import_all_prompt_presets(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    body: PromptPresetImportAllRequest,
) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)
    return ok_payload(request_id=request_id, data=build_prompt_import_all_payload(db, project_id=project_id, body=body))


@router.post("/projects/{project_id}/prompt_preview")
def preview_prompt(request: Request, db: DbDep, user_id: UserIdDep, project_id: str, body: PromptPreviewRequest) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)
    return ok_payload(
        request_id=request_id,
        data=build_prompt_preview_response(db, project_id=project_id, request_id=request_id, body=body),
    )
