from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.project import Project
from app.services.chapter_context_service import build_post_edit_render_values
from app.services.generation_service import PreparedLlmCall, call_llm_and_record, with_param_overrides
from app.services.llm_task_preset_resolver import resolve_task_llm_config
from app.services.mcp.service import McpResearchConfig, McpToolCallResult, run_mcp_research_and_record
from app.services.output_contracts import contract_for_task
from app.services.post_edit_validation import validate_content_optimize_output, validate_post_edit_output
from app.services.prompt_presets import render_preset_for_task


@dataclass(frozen=True, slots=True)
class PostEditStepResult:
    applied: bool
    run_id: str
    edited_content_md: str
    warnings: list[str]
    parse_error: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class ContentOptimizeStepResult:
    applied: bool
    run_id: str
    optimized_content_md: str
    warnings: list[str]
    parse_error: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class PlanStepResult:
    plan_out: dict[str, object]
    warnings: list[str]
    parse_error: dict[str, object] | None
    finish_reason: str | None


@dataclass(frozen=True, slots=True)
class ChapterGenerateStepResult:
    data: dict[str, object]
    warnings: list[str]
    parse_error: dict[str, object] | None
    finish_reason: str | None
    dropped_params: list[str]
    latency_ms: int
    run_id: str


@dataclass(frozen=True, slots=True)
class McpResearchStepResult:
    applied: bool
    context_md: str
    tool_runs: list[McpToolCallResult]
    warnings: list[str]


@dataclass(frozen=True, slots=True)
class _RewriteStepResult:
    applied: bool
    run_id: str
    content_md: str
    warnings: list[str]
    parse_error: dict[str, object] | None


def run_mcp_research_step(
    *,
    logger: logging.Logger,
    request_id: str,
    actor_user_id: str,
    project_id: str,
    chapter_id: str | None,
    config: McpResearchConfig,
) -> McpResearchStepResult:
    if not config.enabled:
        return McpResearchStepResult(applied=False, context_md="", tool_runs=[], warnings=[])

    try:
        context, results, warnings = run_mcp_research_and_record(
            request_id=request_id,
            actor_user_id=actor_user_id,
            project_id=project_id,
            chapter_id=chapter_id,
            config=config,
        )
        return McpResearchStepResult(
            applied=bool(context.strip()),
            context_md=context,
            tool_runs=results,
            warnings=warnings,
        )
    except Exception:
        logger.exception("mcp_research_step_failed")
        return McpResearchStepResult(
            applied=False,
            context_md="",
            tool_runs=[],
            warnings=["mcp_research_failed"],
        )


def _run_rewrite_step(
    *,
    logger: logging.Logger,
    request_id: str,
    actor_user_id: str,
    project_id: str,
    chapter_id: str | None,
    api_key: str,
    llm_call: PreparedLlmCall,
    render_values: dict[str, object],
    raw_content: str,
    macro_seed: str,
    task_key: Literal["post_edit", "content_optimize"],
    temperature: float,
    validate_output: Callable[[str, str], list[str]],
    run_type: str,
    render_value_overrides: dict[str, object] | None = None,
    run_params_extra_json: dict[str, object] | None = None,
) -> _RewriteStepResult:
    effective_llm_call = llm_call
    effective_api_key = str(api_key)
    config_warnings: list[str] = []
    with SessionLocal() as db:
        project = db.get(Project, project_id)
        if project is not None:
            try:
                resolved = resolve_task_llm_config(
                    db,
                    project=project,
                    user_id=actor_user_id,
                    task_key=task_key,
                    header_api_key=None,
                )
            except Exception as exc:
                logger.warning(
                    "llm_config_resolve_failed task=%s error_type=%s",
                    task_key,
                    type(exc).__name__,
                )
                config_warnings.append("llm_config_resolve_failed")
                resolved = None
            if resolved is not None:
                effective_llm_call = resolved.llm_call
                effective_api_key = str(resolved.api_key)

        values = build_post_edit_render_values(render_values, raw_content=raw_content)
        if render_value_overrides:
            values.update(render_value_overrides)

        prompt_system, prompt_user, prompt_messages, _, _, _, render_log = render_preset_for_task(
            db,
            project_id=project_id,
            task=task_key,
            values=values,  # type: ignore[arg-type]
            macro_seed=macro_seed,
            provider=effective_llm_call.provider,
        )
    render_log_json = json.dumps(render_log, ensure_ascii=False)

    rewrite_call = with_param_overrides(effective_llm_call, {"temperature": temperature})
    rewrite_result = call_llm_and_record(
        logger=logger,
        request_id=request_id,
        actor_user_id=actor_user_id,
        project_id=project_id,
        chapter_id=chapter_id,
        run_type=run_type,
        api_key=effective_api_key,
        prompt_system=prompt_system,
        prompt_user=prompt_user,
        prompt_messages=prompt_messages,
        prompt_render_log_json=render_log_json,
        llm_call=rewrite_call,
        run_params_extra_json=run_params_extra_json,
    )

    contract = contract_for_task(task_key)
    parsed = contract.parse(rewrite_result.text, finish_reason=rewrite_result.finish_reason)
    warnings = [*config_warnings, *parsed.warnings]
    parse_error = parsed.parse_error
    content = str(parsed.data.get("content_md") or "").strip()
    applied = parse_error is None and bool(content)
    if applied:
        extra_warnings = validate_output(raw_content, content)
        if extra_warnings:
            warnings.extend(extra_warnings)
            applied = False
    if not applied:
        warnings.append(f"{task_key}_failed")

    return _RewriteStepResult(
        applied=applied,
        run_id=rewrite_result.run_id,
        content_md=content,
        warnings=warnings,
        parse_error=parse_error,
    )


def run_post_edit_step(
    *,
    logger: logging.Logger,
    request_id: str,
    actor_user_id: str,
    project_id: str,
    chapter_id: str | None,
    api_key: str,
    llm_call: PreparedLlmCall,
    render_values: dict[str, object],
    raw_content: str,
    macro_seed: str,
    post_edit_sanitize: bool = False,
    run_params_extra_json: dict[str, object] | None = None,
) -> PostEditStepResult:
    result = _run_rewrite_step(
        logger=logger,
        request_id=request_id,
        actor_user_id=actor_user_id,
        project_id=project_id,
        chapter_id=chapter_id,
        api_key=api_key,
        llm_call=llm_call,
        render_values=render_values,
        raw_content=raw_content,
        macro_seed=macro_seed,
        task_key="post_edit",
        temperature=0.4,
        validate_output=lambda source, output: validate_post_edit_output(
            raw_content=source,
            edited_content=output,
        ),
        render_value_overrides={"post_edit_sanitize": bool(post_edit_sanitize)},
        run_type="post_edit_sanitize" if post_edit_sanitize else "post_edit",
        run_params_extra_json=run_params_extra_json,
    )
    return PostEditStepResult(
        applied=result.applied,
        run_id=result.run_id,
        edited_content_md=result.content_md,
        warnings=result.warnings,
        parse_error=result.parse_error,
    )


def run_content_optimize_step(
    *,
    logger: logging.Logger,
    request_id: str,
    actor_user_id: str,
    project_id: str,
    chapter_id: str | None,
    api_key: str,
    llm_call: PreparedLlmCall,
    render_values: dict[str, object],
    raw_content: str,
    macro_seed: str,
    run_params_extra_json: dict[str, object] | None = None,
) -> ContentOptimizeStepResult:
    result = _run_rewrite_step(
        logger=logger,
        request_id=request_id,
        actor_user_id=actor_user_id,
        project_id=project_id,
        chapter_id=chapter_id,
        api_key=api_key,
        llm_call=llm_call,
        render_values=render_values,
        raw_content=raw_content,
        macro_seed=macro_seed,
        task_key="content_optimize",
        temperature=0.35,
        validate_output=lambda source, output: validate_content_optimize_output(
            raw_content=source,
            optimized_content=output,
        ),
        run_type="content_optimize",
        run_params_extra_json=run_params_extra_json,
    )
    return ContentOptimizeStepResult(
        applied=result.applied,
        run_id=result.run_id,
        optimized_content_md=result.content_md,
        warnings=result.warnings,
        parse_error=result.parse_error,
    )


def run_plan_llm_step(
    *,
    logger: logging.Logger,
    request_id: str,
    actor_user_id: str,
    project_id: str,
    chapter_id: str | None,
    api_key: str,
    llm_call: PreparedLlmCall,
    prompt_system: str,
    prompt_user: str,
    prompt_messages: list,
    prompt_render_log_json: str | None,
    run_params_extra_json: dict[str, object] | None = None,
) -> PlanStepResult:
    plan_call = with_param_overrides(llm_call, {"temperature": 0.2, "max_tokens": 1024})
    plan_result = call_llm_and_record(
        logger=logger,
        request_id=request_id,
        actor_user_id=actor_user_id,
        project_id=project_id,
        chapter_id=chapter_id,
        run_type="plan_chapter",
        api_key=api_key,
        prompt_system=prompt_system,
        prompt_user=prompt_user,
        prompt_messages=prompt_messages,
        prompt_render_log_json=prompt_render_log_json,
        llm_call=plan_call,
        run_params_extra_json=run_params_extra_json,
    )

    plan_contract = contract_for_task("plan_chapter")
    parsed = plan_contract.parse(plan_result.text, finish_reason=plan_result.finish_reason)
    return PlanStepResult(
        plan_out=parsed.data,
        warnings=list(parsed.warnings),
        parse_error=parsed.parse_error,
        finish_reason=plan_result.finish_reason,
    )


def run_chapter_generate_llm_step(
    *,
    logger: logging.Logger,
    request_id: str,
    actor_user_id: str,
    project_id: str,
    chapter_id: str | None,
    run_type: str,
    api_key: str,
    llm_call: PreparedLlmCall,
    prompt_system: str,
    prompt_user: str,
    prompt_messages: list,
    prompt_render_log_json: str | None,
    run_params_extra_json: dict[str, object] | None = None,
) -> ChapterGenerateStepResult:
    llm_result = call_llm_and_record(
        logger=logger,
        request_id=request_id,
        actor_user_id=actor_user_id,
        project_id=project_id,
        chapter_id=chapter_id,
        run_type=run_type,
        api_key=api_key,
        prompt_system=prompt_system,
        prompt_user=prompt_user,
        prompt_messages=prompt_messages,
        prompt_render_log_json=prompt_render_log_json,
        llm_call=llm_call,
        run_params_extra_json=run_params_extra_json,
    )

    chapter_contract = contract_for_task("chapter_generate")
    parsed = chapter_contract.parse(llm_result.text, finish_reason=llm_result.finish_reason)
    return ChapterGenerateStepResult(
        data=parsed.data,
        warnings=list(parsed.warnings),
        parse_error=parsed.parse_error,
        finish_reason=llm_result.finish_reason,
        dropped_params=list(llm_result.dropped_params),
        latency_ms=int(llm_result.latency_ms),
        run_id=llm_result.run_id,
    )
