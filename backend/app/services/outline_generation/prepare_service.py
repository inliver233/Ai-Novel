from __future__ import annotations

import json

from sqlalchemy import select

from app.api.deps import require_project_editor
from app.core.errors import AppError
from app.db.session import SessionLocal
from app.models.character import Character
from app.models.project_settings import ProjectSettings
from app.schemas.outline_generate import OutlineGenerateRequest
from app.services.generation_service import PreparedLlmCall, build_run_params_json, with_param_overrides
from app.services.llm_task_preset_resolver import resolve_task_llm_config
from app.services.outline_generation.models import PreparedOutlineGeneration
from app.services.outline_generation.policy import (
    _build_outline_generation_guidance,
    _extract_target_chapter_count,
    _recommend_outline_max_tokens,
)
from app.services.prompt_presets import render_preset_for_task
from app.services.prompt_store import format_characters
from app.services.run_store import write_generation_run
from app.services.style_resolution_service import resolve_style_guide


def prepare_outline_generation(
    *,
    project_id: str,
    body: OutlineGenerateRequest,
    user_id: str,
    request_id: str,
    x_llm_provider: str | None,
    x_llm_api_key: str | None,
) -> PreparedOutlineGeneration:

    with SessionLocal() as db:
        project = require_project_editor(db, project_id=project_id, user_id=user_id)
        resolved_outline = resolve_task_llm_config(
            db,
            project=project,
            user_id=user_id,
            task_key="outline_generate",
            header_api_key=x_llm_api_key,
        )
        if resolved_outline is None:
            raise AppError(code="LLM_CONFIG_ERROR", message="请先在 Prompts 页保存 LLM 配置", status_code=400)
        if x_llm_api_key and x_llm_provider and resolved_outline.llm_call.provider != x_llm_provider:
            raise AppError(code="LLM_CONFIG_ERROR", message="当前任务 provider 与请求头不一致，请先保存/切换", status_code=400)
        resolved_api_key = str(resolved_outline.api_key)

        settings_row = db.get(ProjectSettings, project_id)
        world_setting = (settings_row.world_setting if settings_row else "") or ""
        settings_style_guide = (settings_row.style_guide if settings_row else "") or ""
        constraints = (settings_row.constraints if settings_row else "") or ""

        style_resolution: dict[str, object] = {"style_id": None, "source": "disabled"}
        if not body.context.include_world_setting:
            world_setting = ""
            settings_style_guide = ""
            constraints = ""
        else:
            resolved_style_guide, style_resolution = resolve_style_guide(
                db,
                project_id=project_id,
                user_id=user_id,
                requested_style_id=body.style_id,
                include_style_guide=True,
                settings_style_guide=settings_style_guide,
            )
            settings_style_guide = resolved_style_guide

        run_params_extra_json: dict[str, object] = {"style_resolution": style_resolution}

        chars: list[Character] = []
        if body.context.include_characters:
            chars = db.execute(select(Character).where(Character.project_id == project_id)).scalars().all()
        characters_text = format_characters(chars)
        target_chapter_count = _extract_target_chapter_count(body.requirements)
        guidance = _build_outline_generation_guidance(target_chapter_count)

        requirements_text = json.dumps(body.requirements or {}, ensure_ascii=False, indent=2)
        values: dict[str, object] = {
            "project_name": project.name or "",
            "genre": project.genre or "",
            "logline": project.logline or "",
            "world_setting": world_setting,
            "style_guide": settings_style_guide,
            "constraints": constraints,
            "characters": characters_text,
            "outline": "",
            "chapter_number": "",
            "chapter_title": "",
            "chapter_plan": "",
            "requirements": requirements_text,
            "instruction": "",
            "previous_chapter": "",
            "target_chapter_count": target_chapter_count or "",
            "chapter_count_rule": guidance.get("chapter_count_rule", ""),
            "chapter_detail_rule": guidance.get("chapter_detail_rule", ""),
        }

        prompt_system, prompt_user, prompt_messages, _, _, _, render_log = render_preset_for_task(
            db,
            project_id=project_id,
            task="outline_generate",
            values=values,
            macro_seed=request_id,
            provider=resolved_outline.llm_call.provider,
        )
        prompt_render_log_json = json.dumps(render_log, ensure_ascii=False)

        llm_call = resolved_outline.llm_call
        current_max_tokens = llm_call.params.get("max_tokens")
        current_max_tokens_int = int(current_max_tokens) if isinstance(current_max_tokens, int) else None
        wanted_max_tokens = _recommend_outline_max_tokens(
            target_chapter_count=target_chapter_count,
            provider=llm_call.provider,
            model=llm_call.model,
            current_max_tokens=current_max_tokens_int,
        )
        if isinstance(wanted_max_tokens, int) and wanted_max_tokens > 0:
            llm_call = with_param_overrides(llm_call, {"max_tokens": wanted_max_tokens})
            run_params_extra_json["outline_auto_max_tokens"] = {
                "target_chapter_count": target_chapter_count,
                "from": current_max_tokens_int,
                "to": wanted_max_tokens,
            }

    run_params_json = build_run_params_json(
        params_json=llm_call.params_json,
        memory_retrieval_log_json=None,
        extra_json=run_params_extra_json,
    )
    return PreparedOutlineGeneration(
        resolved_api_key=resolved_api_key,
        prompt_system=prompt_system,
        prompt_user=prompt_user,
        prompt_messages=prompt_messages,
        prompt_render_log_json=prompt_render_log_json,
        llm_call=llm_call,
        target_chapter_count=target_chapter_count,
        run_params_extra_json=run_params_extra_json,
        run_params_json=run_params_json,
    )

def _build_outline_segment_aggregate_output_text(
    *,
    data: dict[str, object],
    warnings: list[str],
    meta: dict[str, object],
) -> str:
    chapters = data.get("chapters")
    chapter_count = len(chapters) if isinstance(chapters, list) else 0
    coverage = data.get("chapter_coverage")
    summary: dict[str, object] = {
        "mode": "segmented",
        "chapter_count": chapter_count,
        "warnings": warnings[:40],
        "segmented_generation": meta,
    }
    if isinstance(coverage, dict):
        summary["chapter_coverage"] = {
            "target_chapter_count": coverage.get("target_chapter_count"),
            "missing_count": coverage.get("missing_count"),
            "missing_numbers_preview": (coverage.get("missing_numbers") or [])[:30]
            if isinstance(coverage.get("missing_numbers"), list)
            else [],
        }
    return json.dumps(summary, ensure_ascii=False)

def _write_outline_segmented_aggregate_run(
    *,
    request_id: str,
    actor_user_id: str,
    project_id: str,
    run_type: str,
    llm_call: PreparedLlmCall,
    prompt_system: str,
    prompt_user: str,
    prompt_render_log_json: str | None,
    run_params_json: str,
    data: dict[str, object],
    warnings: list[str],
    parse_error: dict[str, object] | None,
    segmented_run_ids: list[str],
    meta: dict[str, object],
) -> str:
    output_text = _build_outline_segment_aggregate_output_text(data=data, warnings=warnings, meta=meta)
    error_json: str | None = None
    if parse_error is not None:
        error_payload = {
            "code": str(parse_error.get("code") or "OUTLINE_PARSE_ERROR"),
            "message": str(parse_error.get("message") or "分段生成结果不完整"),
            "details": {
                "segmented_run_ids": segmented_run_ids,
                "segmented_generation": meta,
            },
        }
        error_json = json.dumps(error_payload, ensure_ascii=False)
    return write_generation_run(
        request_id=request_id,
        actor_user_id=actor_user_id,
        project_id=project_id,
        chapter_id=None,
        run_type=run_type,
        provider=llm_call.provider,
        model=llm_call.model,
        prompt_system=prompt_system,
        prompt_user=prompt_user,
        prompt_render_log_json=prompt_render_log_json,
        params_json=run_params_json,
        output_text=output_text,
        error_json=error_json,
    )
