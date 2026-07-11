from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_chapter_editor
from app.core.errors import AppError
from app.db.session import SessionLocal
from app.models.chapter import Chapter
from app.models.project import Project
from app.models.project_settings import ProjectSettings
from app.schemas.chapter_generate import ChapterGenerateRequest
from app.services.chapter_context_service import build_chapter_generate_render_values
from app.services.chapter_generation.memory_service import (
    build_memory_run_params_extra_json,
    prepare_chapter_memory_injection,
)
from app.services.chapter_generation.models import PreparedChapterGenerateRequest
from app.services.chapter_generation.prompt_service import (
    apply_prompt_override,
    build_mcp_research_config,
    build_mcp_research_params,
    build_prompt_inspector_params,
    inject_mcp_research_into_values,
    resolve_macro_seed,
)
from app.services.generation_pipeline import run_mcp_research_step
from app.services.generation_service import build_run_params_json, prepare_llm_call
from app.services.llm_task_preset_resolver import resolve_task_llm_config, resolve_task_preset
from app.services.prompt_presets import render_preset_for_task


def resolve_task_llm_for_call(
    *,
    db: Session,
    project: Project,
    user_id: str,
    task_key: str,
    x_llm_provider: str | None,
    x_llm_api_key: str | None,
):
    resolved = resolve_task_llm_config(
        db,
        project=project,
        user_id=user_id,
        task_key=task_key,
        header_api_key=x_llm_api_key,
    )
    if resolved is None:
        raise AppError(code="LLM_CONFIG_ERROR", message="请先在 Prompts 页保存 LLM 配置", status_code=400)
    if x_llm_api_key and x_llm_provider and resolved.llm_call.provider != x_llm_provider:
        raise AppError(
            code="LLM_CONFIG_ERROR", message="当前任务 provider 与请求头不一致，请先保存/切换", status_code=400
        )
    return resolved


def find_missing_prereq_numbers(
    db: Session,
    *,
    project_id: str,
    outline_id: str,
    chapter_number: int,
) -> list[int]:
    if chapter_number <= 1:
        return []

    rows = db.execute(
        select(Chapter.number, Chapter.content_md, Chapter.summary)
        .where(
            Chapter.project_id == project_id,
            Chapter.outline_id == outline_id,
            Chapter.number < chapter_number,
        )
        .order_by(Chapter.number.asc())
    ).all()

    existing: dict[int, tuple[str | None, str | None]] = {int(r[0]): (r[1], r[2]) for r in rows}
    missing: list[int] = []
    for n in range(1, int(chapter_number)):
        content_md, summary = existing.get(n, (None, None))
        if not ((content_md or "").strip() or (summary or "").strip()):
            missing.append(n)
    return missing


def _require_chapter_prereqs_if_needed(*, db: Session, chapter: Chapter, body_context: object) -> None:
    if not bool(getattr(body_context, "require_sequential", False)):
        return
    missing_numbers = find_missing_prereq_numbers(
        db,
        project_id=str(chapter.project_id),
        outline_id=str(chapter.outline_id),
        chapter_number=int(chapter.number),
    )
    if missing_numbers:
        raise AppError(
            code="CHAPTER_PREREQ_MISSING",
            message=f"缺少前置章节内容：第 {', '.join(str(n) for n in missing_numbers)} 章",
            status_code=400,
            details={"missing_numbers": missing_numbers},
        )


def prepare_chapter_generate_request(
    *,
    logger: logging.Logger,
    request_id: str,
    chapter_id: str,
    body: ChapterGenerateRequest,
    user_id: str,
    x_llm_provider: str | None,
    x_llm_api_key: str | None,
    require_api_key: bool,
) -> PreparedChapterGenerateRequest:
    macro_seed = resolve_macro_seed(request_id=request_id, body=body)
    with SessionLocal() as db:
        chapter = require_chapter_editor(db, chapter_id=chapter_id, user_id=user_id)
        _require_chapter_prereqs_if_needed(db=db, chapter=chapter, body_context=body.context)
        project_id = str(chapter.project_id)
        project = db.get(Project, project_id)
        if project is None:
            raise AppError.not_found()

        if require_api_key:
            resolved_chapter = resolve_task_llm_for_call(
                db=db,
                project=project,
                user_id=user_id,
                task_key="chapter_generate",
                x_llm_provider=x_llm_provider,
                x_llm_api_key=x_llm_api_key,
            )
            llm_call = resolved_chapter.llm_call
            resolved_api_key = str(resolved_chapter.api_key)
        else:
            preset_row, _ = resolve_task_preset(db, project_id=project_id, task_key="chapter_generate")
            if preset_row is None:
                raise AppError(code="LLM_CONFIG_ERROR", message="请先在 Prompts 页保存 LLM 配置", status_code=400)
            llm_call = prepare_llm_call(preset_row)
            if x_llm_api_key and x_llm_provider and llm_call.provider != x_llm_provider:
                raise AppError(
                    code="LLM_CONFIG_ERROR", message="当前任务 provider 与请求头不一致，请先保存/切换", status_code=400
                )
            resolved_api_key = ""

        values, base_instruction, requirements_obj, style_resolution = build_chapter_generate_render_values(
            db,
            project=project,
            chapter=chapter,
            body=body,
            user_id=user_id,
        )
        settings_row = db.get(ProjectSettings, project_id)
        context_optimizer_enabled = bool(getattr(settings_row, "context_optimizer_enabled", False))
        values["context_optimizer_enabled"] = context_optimizer_enabled
        memory_preparation = prepare_chapter_memory_injection(
            db=db,
            project_id=project_id,
            chapter=chapter,
            body=body,
            settings_row=settings_row,
            base_instruction=base_instruction,
            values=values,
        )
        run_params_extra_json = build_memory_run_params_extra_json(
            style_resolution=style_resolution,
            memory_injection_enabled=body.memory_injection_enabled,
            memory_preparation=memory_preparation,
        )

        mcp_cfg = build_mcp_research_config(body)
        mcp_step = run_mcp_research_step(
            logger=logger,
            request_id=request_id,
            actor_user_id=user_id,
            project_id=project_id,
            chapter_id=chapter_id,
            config=mcp_cfg,
        )
        inject_mcp_research_into_values(values=values, context_md=mcp_step.context_md)
        mcp_research = None
        if mcp_cfg.enabled or mcp_step.warnings:
            run_params_extra_json = run_params_extra_json or {}
            mcp_research = build_mcp_research_params(
                cfg=mcp_cfg,
                applied=mcp_step.applied,
                tool_run_ids=[r.run_id for r in mcp_step.tool_runs],
                warnings=mcp_step.warnings,
            )
            run_params_extra_json["mcp_research"] = mcp_research

        prepared = PreparedChapterGenerateRequest(
            request_id=request_id,
            chapter_id=chapter_id,
            project_id=project_id,
            macro_seed=macro_seed,
            resolved_api_key=resolved_api_key,
            llm_call=llm_call,
            render_values=values,
            run_params_extra_json=run_params_extra_json,
            base_instruction=base_instruction,
            requirements_obj=requirements_obj,
            context_optimizer_enabled=context_optimizer_enabled,
            style_resolution=style_resolution,
            memory_preparation=memory_preparation,
            mcp_research=mcp_research,
        )

        if body.plan_first:
            resolved_plan = resolve_task_llm_for_call(
                db=db,
                project=project,
                user_id=user_id,
                task_key="plan_chapter",
                x_llm_provider=x_llm_provider,
                x_llm_api_key=x_llm_api_key,
            )
            plan_values = dict(values)
            plan_values["instruction"] = base_instruction
            plan_values["user"] = {"instruction": base_instruction, "requirements": requirements_obj}
            plan_prompt_system, plan_prompt_user, plan_prompt_messages, _, _, _, plan_render_log = (
                render_preset_for_task(
                    db,
                    project_id=project_id,
                    task="plan_chapter",
                    values=plan_values,  # type: ignore[arg-type]
                    macro_seed=f"{macro_seed}:plan",
                    provider=resolved_plan.llm_call.provider,
                )
            )
            prepared.plan_prompt_system = plan_prompt_system
            prepared.plan_prompt_user = plan_prompt_user
            prepared.plan_prompt_messages = plan_prompt_messages
            prepared.plan_prompt_render_log_json = json.dumps(plan_render_log, ensure_ascii=False)
            prepared.plan_llm_call = resolved_plan.llm_call
            prepared.plan_api_key = str(resolved_plan.api_key)
        else:
            render_main_prompt(prepared=prepared, body=body, values=values)

        prepared.run_params_json = build_run_params_json(
            params_json=prepared.llm_call.params_json,
            memory_retrieval_log_json=None,
            extra_json=prepared.run_params_extra_json,
        )
    return prepared


def render_main_prompt(
    *,
    prepared: PreparedChapterGenerateRequest,
    body: ChapterGenerateRequest,
    values: dict[str, object],
) -> None:
    with SessionLocal() as db:
        prompt_system, prompt_user, prompt_messages, _, _, _, render_log = render_preset_for_task(
            db,
            project_id=prepared.project_id,
            task="chapter_generate",
            values=values,  # type: ignore[arg-type]
            macro_seed=prepared.macro_seed,
            provider=prepared.llm_call.provider,
        )
    precheck_prompt_system = prompt_system
    precheck_prompt_user = prompt_user
    precheck_prompt_messages = prompt_messages
    prompt_system, prompt_user, prompt_messages, override_applied = apply_prompt_override(
        prompt_system=prompt_system,
        prompt_user=prompt_user,
        prompt_messages=prompt_messages,
        body=body,
    )
    prepared.prompt_system = prompt_system
    prepared.prompt_user = prompt_user
    prepared.prompt_messages = prompt_messages
    prepared.prompt_render_log = render_log
    prepared.prompt_render_log_json = json.dumps(render_log, ensure_ascii=False)
    prepared.prompt_overridden = bool(override_applied)
    prepared.run_params_extra_json = prepared.run_params_extra_json or {}
    prepared.run_params_extra_json["prompt_inspector"] = build_prompt_inspector_params(
        macro_seed=prepared.macro_seed,
        prompt_overridden=override_applied,
        body=body,
        precheck_prompt_system=precheck_prompt_system,
        precheck_prompt_user=precheck_prompt_user,
        precheck_prompt_messages=precheck_prompt_messages,
        final_prompt_system=prompt_system,
        final_prompt_user=prompt_user,
        final_prompt_messages=prompt_messages,
    )
    prepared.run_params_json = build_run_params_json(
        params_json=prepared.llm_call.params_json,
        memory_retrieval_log_json=None,
        extra_json=prepared.run_params_extra_json,
    )
