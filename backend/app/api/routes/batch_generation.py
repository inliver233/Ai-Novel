from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request

from app.api.deps import DbDep, UserIdDep, require_project_editor, require_project_viewer
from app.core.errors import ok_payload
from app.schemas.batch_generation import BatchGenerationCreateRequest, BatchGenerationTaskItemOut, BatchGenerationTaskOut
from app.services.batch_generation_application import (
    create_batch_generation_task as create_batch_generation_task_application,
    load_latest_batch_generation_snapshot,
)
from app.services.batch_generation_commands import (
    BatchGenerationCommandResult,
    BatchGenerationSnapshot,
    cancel_batch_generation_task as cancel_batch_generation_task_command,
    load_batch_generation_project_id,
    load_batch_generation_snapshot,
    pause_batch_generation_task as pause_batch_generation_task_command,
    resume_batch_generation_task as resume_batch_generation_task_command,
    retry_failed_batch_generation_task as retry_failed_batch_generation_task_command,
    skip_failed_batch_generation_task as skip_failed_batch_generation_task_command,
)

router = APIRouter()


def _serialize_batch_snapshot(snapshot: BatchGenerationSnapshot) -> dict:
    return {
        "task": BatchGenerationTaskOut.model_validate(snapshot.task).model_dump(),
        "items": [BatchGenerationTaskItemOut.model_validate(item).model_dump() for item in snapshot.items],
    }


def _serialize_requeue_result(result: BatchGenerationCommandResult, *, flag: str) -> dict:
    if result.applied:
        return {**_serialize_batch_snapshot(result.snapshot), flag: True}
    return {
        "task": BatchGenerationTaskOut.model_validate(result.snapshot.task).model_dump(),
        flag: False,
    }


@router.post("/projects/{project_id}/batch_generation_tasks")
def create_batch_generation_task(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    body: BatchGenerationCreateRequest,
    _x_llm_provider: Annotated[str | None, Header(alias="X-LLM-Provider", max_length=64)] = None,
) -> dict:
    request_id = request.state.request_id
    snapshot = create_batch_generation_task_application(
        db,
        user_id=user_id,
        project_id=project_id,
        body=body,
        request_id=request_id,
    )
    return ok_payload(request_id=request_id, data=_serialize_batch_snapshot(snapshot))


@router.get("/projects/{project_id}/batch_generation_tasks/active")
def get_active_batch_generation_task(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
) -> dict:
    request_id = request.state.request_id
    require_project_viewer(db, project_id=project_id, user_id=user_id)
    snapshot = load_latest_batch_generation_snapshot(db, project_id=project_id)
    data = _serialize_batch_snapshot(snapshot) if snapshot is not None else {"task": None, "items": []}
    return ok_payload(request_id=request_id, data=data)


@router.get("/batch_generation_tasks/{task_id}")
def get_batch_generation_task(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    task_id: str,
) -> dict:
    request_id = request.state.request_id
    project_id = load_batch_generation_project_id(db, task_id=task_id)
    require_project_viewer(db, project_id=project_id, user_id=user_id)
    snapshot = load_batch_generation_snapshot(db, task_id=task_id)
    return ok_payload(request_id=request_id, data=_serialize_batch_snapshot(snapshot))


@router.post("/batch_generation_tasks/{task_id}/pause")
def pause_batch_generation_task(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    task_id: str,
) -> dict:
    request_id = request.state.request_id
    project_id = load_batch_generation_project_id(db, task_id=task_id)
    require_project_editor(db, project_id=project_id, user_id=user_id)
    result = pause_batch_generation_task_command(db, task_id=task_id, authorized_project_id=project_id)
    return ok_payload(
        request_id=request_id,
        data={"task": BatchGenerationTaskOut.model_validate(result.snapshot.task).model_dump(), "paused": result.applied},
    )


@router.post("/batch_generation_tasks/{task_id}/resume")
def resume_batch_generation_task(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    task_id: str,
) -> dict:
    request_id = request.state.request_id
    project_id = load_batch_generation_project_id(db, task_id=task_id)
    require_project_editor(db, project_id=project_id, user_id=user_id)
    result = resume_batch_generation_task_command(
        db,
        task_id=task_id,
        authorized_project_id=project_id,
        actor_user_id=user_id,
    )
    return ok_payload(request_id=request_id, data=_serialize_requeue_result(result, flag="resumed"))


@router.post("/batch_generation_tasks/{task_id}/retry_failed")
def retry_failed_batch_generation_task(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    task_id: str,
) -> dict:
    request_id = request.state.request_id
    project_id = load_batch_generation_project_id(db, task_id=task_id)
    require_project_editor(db, project_id=project_id, user_id=user_id)
    result = retry_failed_batch_generation_task_command(
        db,
        task_id=task_id,
        authorized_project_id=project_id,
        actor_user_id=user_id,
    )
    return ok_payload(request_id=request_id, data=_serialize_requeue_result(result, flag="retried"))


@router.post("/batch_generation_tasks/{task_id}/skip_failed")
def skip_failed_batch_generation_task(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    task_id: str,
) -> dict:
    request_id = request.state.request_id
    project_id = load_batch_generation_project_id(db, task_id=task_id)
    require_project_editor(db, project_id=project_id, user_id=user_id)
    result = skip_failed_batch_generation_task_command(
        db,
        task_id=task_id,
        authorized_project_id=project_id,
        actor_user_id=user_id,
    )
    return ok_payload(request_id=request_id, data=_serialize_requeue_result(result, flag="skipped"))


@router.post("/batch_generation_tasks/{task_id}/cancel")
def cancel_batch_generation_task(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    task_id: str,
) -> dict:
    request_id = request.state.request_id
    project_id = load_batch_generation_project_id(db, task_id=task_id)
    require_project_editor(db, project_id=project_id, user_id=user_id)
    result = cancel_batch_generation_task_command(db, task_id=task_id, authorized_project_id=project_id)
    return ok_payload(
        request_id=request_id,
        data={"task": BatchGenerationTaskOut.model_validate(result.snapshot.task).model_dump(), "canceled": result.applied},
    )
