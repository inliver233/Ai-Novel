from __future__ import annotations

import json

from sqlalchemy import select

from app.api.deps import require_chapter_editor
from app.core.errors import AppError
from app.db.session import SessionLocal
from app.models.character import Character
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_settings import ProjectSettings
from app.schemas.chapter_plan import ChapterPlanRequest
from app.services.chapter_context_service import load_detailed_outline_context, load_previous_chapter_context
from app.services.chapter_generation.models import PreparedChapterPlanRequest
from app.services.chapter_generation.prepare_service import (
    _require_chapter_prereqs_if_needed,
    resolve_task_llm_for_call,
)
from app.services.chapter_generation.prompt_service import resolve_macro_seed
from app.services.prompt_presets import render_preset_for_task
from app.services.prompt_store import format_characters


def prepare_chapter_plan_request(
    *,
    request_id: str,
    chapter_id: str,
    body: ChapterPlanRequest,
    user_id: str,
    x_llm_provider: str | None,
    x_llm_api_key: str | None,
) -> PreparedChapterPlanRequest:
    with SessionLocal() as db:
        chapter = require_chapter_editor(db, chapter_id=chapter_id, user_id=user_id)
        _require_chapter_prereqs_if_needed(db=db, chapter=chapter, body_context=body.context)
        project_id = str(chapter.project_id)
        project = db.get(Project, project_id)
        if project is None:
            raise AppError.not_found()

        resolved_plan = resolve_task_llm_for_call(
            db=db,
            project=project,
            user_id=user_id,
            task_key="plan_chapter",
            x_llm_provider=x_llm_provider,
            x_llm_api_key=x_llm_api_key,
        )
        settings_row = db.get(ProjectSettings, project_id)
        outline_row = db.get(Outline, chapter.outline_id)

        world_setting = (settings_row.world_setting if settings_row else "") or ""
        style_guide = (settings_row.style_guide if settings_row else "") or ""
        constraints = (settings_row.constraints if settings_row else "") or ""

        if not body.context.include_world_setting:
            world_setting = ""
        if not body.context.include_style_guide:
            style_guide = ""
        if not body.context.include_constraints:
            constraints = ""

        outline_text = (outline_row.content_md if outline_row else "") or ""
        if not body.context.include_outline:
            outline_text = ""

        chars: list[Character] = []
        if body.context.character_ids:
            chars = (
                db.execute(
                    select(Character).where(
                        Character.project_id == project_id,
                        Character.id.in_(body.context.character_ids),
                    )
                )
                .scalars()
                .all()
            )
        characters_text = format_characters(chars)

        prev_text, prev_ending = load_previous_chapter_context(
            db,
            project_id=project_id,
            outline_id=str(chapter.outline_id),
            chapter_number=int(chapter.number),
            previous_chapter=body.context.previous_chapter,
        )

        detailed_outline_ctx = load_detailed_outline_context(
            chapter_number=int(chapter.number),
            outline_id=str(chapter.outline_id),
            db=db,
        )

        values: dict[str, object] = {
            "project_name": project.name or "",
            "genre": project.genre or "",
            "logline": project.logline or "",
            "world_setting": world_setting,
            "style_guide": style_guide,
            "constraints": constraints,
            "characters": characters_text,
            "outline": outline_text,
            "chapter_number": str(chapter.number),
            "chapter_title": (chapter.title or ""),
            "chapter_plan": (chapter.plan or ""),
            "instruction": body.instruction.strip(),
            "previous_chapter": prev_text,
            "previous_chapter_ending": prev_ending,
            "detailed_outline_context": detailed_outline_ctx,
        }
        values["project"] = {
            "name": project.name or "",
            "genre": project.genre or "",
            "logline": project.logline or "",
            "world_setting": world_setting,
            "style_guide": style_guide,
            "constraints": constraints,
            "characters": characters_text,
        }
        values["story"] = {
            "outline": outline_text,
            "chapter_number": int(chapter.number),
            "chapter_title": (chapter.title or ""),
            "chapter_plan": (chapter.plan or ""),
            "previous_chapter": prev_text,
            "previous_chapter_ending": prev_ending,
            "detailed_outline_context": detailed_outline_ctx,
        }
        values["user"] = {"instruction": body.instruction.strip()}
        values["context_optimizer_enabled"] = bool(getattr(settings_row, "context_optimizer_enabled", False))

        macro_seed = resolve_macro_seed(request_id=request_id, body=body)
        prompt_system, prompt_user, prompt_messages, _, _, _, render_log = render_preset_for_task(
            db,
            project_id=project_id,
            task="plan_chapter",
            values=values,  # type: ignore[arg-type]
            macro_seed=f"{macro_seed}:plan",
            provider=resolved_plan.llm_call.provider,
        )
        prompt_render_log_json = json.dumps(render_log, ensure_ascii=False)

    return PreparedChapterPlanRequest(
        project_id=project_id,
        resolved_api_key=str(resolved_plan.api_key),
        llm_call=resolved_plan.llm_call,
        prompt_system=prompt_system,
        prompt_user=prompt_user,
        prompt_messages=prompt_messages,
        prompt_render_log_json=prompt_render_log_json,
    )
