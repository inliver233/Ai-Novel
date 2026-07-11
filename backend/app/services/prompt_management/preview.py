from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.prompt_management.mappers import _build_prompt_preview_payload
from app.core.errors import AppError
from app.models.llm_preset import LLMPreset
from app.schemas.prompt_presets import PromptPreviewRequest
from app.services.prompt_presets import render_preset_for_task
from app.services.prompt_task_catalog import PROMPT_TASK_SET


def _build_prompt_preview_response(
    db: Session,
    *,
    project_id: str,
    request_id: str,
    body: PromptPreviewRequest,
) -> dict[str, object]:
    if body.task not in PROMPT_TASK_SET:
        raise AppError.validation(message="不支持的 task")

    llm_preset = db.get(LLMPreset, project_id)
    provider = llm_preset.provider if llm_preset is not None else None

    system, user, _, missing, blocks, preset_id, render_log = render_preset_for_task(
        db,
        project_id=project_id,
        task=body.task,
        values=body.values,
        preset_id=body.preset_id,
        macro_seed=request_id,
        provider=provider,
        allow_autocreate=False,
    )
    return {
        "preview": _build_prompt_preview_payload(
            preset_id=preset_id,
            task=body.task,
            system=system,
            user=user,
            missing=missing,
            blocks=blocks,
            render_log=render_log,
        ),
        "render_log": render_log,
    }
