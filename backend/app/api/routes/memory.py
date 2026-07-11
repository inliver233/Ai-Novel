from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.api.deps import DbDep, UserIdDep, require_project_editor, require_project_viewer
from app.core.errors import ok_payload
from app.schemas.memory_preview import MemoryPreviewRequest
from app.schemas.story_memory_import import StoryMemoryImportV1Request
from app.services.memory_retrieval_service import retrieve_memory_context_pack
from app.services.story_memory_import_service import import_story_memories
router = APIRouter()


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

    data = import_story_memories(
        db,
        project_id=project_id,
        schema_version=body.schema_version,
        items=list(body.memories or []),
        actor_user_id=user_id,
        request_id=request_id,
    )
    return ok_payload(request_id=request_id, data=data)
