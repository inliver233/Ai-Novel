from __future__ import annotations

import logging

from app.core.errors import AppError
from app.schemas.outline_generate import OutlineGenerateRequest
from app.services.generation_service import call_llm_and_record, with_param_overrides
from app.services.outline_generation.chapter_ops import _dedupe_warnings, _enforce_outline_chapter_coverage
from app.services.outline_generation.fill_service import _fill_outline_missing_chapters_with_llm
from app.services.outline_generation.models import OutlineSegmentGenerationResult, PreparedOutlineGeneration
from app.services.outline_generation.policy import _should_use_outline_segmented_mode
from app.services.outline_generation.prepare_service import (
    _build_outline_segment_aggregate_output_text,
    _write_outline_segmented_aggregate_run,
    prepare_outline_generation,
)
from app.services.outline_generation.segment_service import _generate_outline_segmented_with_llm
from app.services.outline_generation.stream_service import generate_outline_stream_events
from app.services.output_contracts import build_repair_prompt_for_task, contract_for_task

logger = logging.getLogger("ainovel")


def generate_outline(
    *,
    request_id: str,
    project_id: str,
    body: OutlineGenerateRequest,
    user_id: str,
    x_llm_provider: str | None,
    x_llm_api_key: str | None,
) -> dict[str, object]:
    prepared = prepare_outline_generation(
        project_id=project_id,
        body=body,
        user_id=user_id,
        request_id=request_id,
        x_llm_provider=x_llm_provider,
        x_llm_api_key=x_llm_api_key,
    )

    if _should_use_outline_segmented_mode(prepared.target_chapter_count):
        assert prepared.target_chapter_count is not None
        segmented = _generate_outline_segmented_with_llm(
            request_id=request_id,
            actor_user_id=user_id,
            project_id=project_id,
            api_key=str(prepared.resolved_api_key),
            llm_call=prepared.llm_call,
            prompt_system=prepared.prompt_system,
            prompt_user=prepared.prompt_user,
            target_chapter_count=prepared.target_chapter_count,
            run_params_extra_json=prepared.run_params_extra_json,
        )
        aggregate_run_id = _write_outline_segmented_aggregate_run(
            request_id=request_id,
            actor_user_id=user_id,
            project_id=project_id,
            run_type="outline_segmented",
            llm_call=prepared.llm_call,
            prompt_system=prepared.prompt_system,
            prompt_user=prepared.prompt_user,
            prompt_render_log_json=prepared.prompt_render_log_json,
            run_params_json=prepared.run_params_json,
            data=segmented.data,
            warnings=segmented.warnings,
            parse_error=segmented.parse_error,
            segmented_run_ids=segmented.run_ids,
            meta=segmented.meta,
        )
        data = dict(segmented.data)
        warnings = _dedupe_warnings(segmented.warnings)
        if warnings:
            data["warnings"] = warnings
        if segmented.parse_error is not None:
            data["parse_error"] = segmented.parse_error
        data["generation_run_id"] = aggregate_run_id
        if segmented.run_ids:
            data["generation_sub_run_ids"] = segmented.run_ids
            data["generation_run_ids"] = [aggregate_run_id, *segmented.run_ids]
        if segmented.latency_ms > 0:
            data["latency_ms"] = segmented.latency_ms
        if segmented.dropped_params:
            data["dropped_params"] = segmented.dropped_params
        if segmented.finish_reasons:
            data["finish_reason"] = segmented.finish_reasons[-1]
            data["finish_reasons"] = segmented.finish_reasons
        data["segmented_generation"] = segmented.meta
        return data

    llm_result = call_llm_and_record(
        logger=logger,
        request_id=request_id,
        actor_user_id=user_id,
        project_id=project_id,
        chapter_id=None,
        run_type="outline",
        api_key=str(prepared.resolved_api_key),
        prompt_system=prepared.prompt_system,
        prompt_user=prepared.prompt_user,
        prompt_messages=prepared.prompt_messages,
        prompt_render_log_json=prepared.prompt_render_log_json,
        llm_call=prepared.llm_call,
        run_params_extra_json=prepared.run_params_extra_json,
    )

    raw_output = llm_result.text
    finish_reason = llm_result.finish_reason
    contract = contract_for_task("outline_generate")
    parsed = contract.parse(raw_output, finish_reason=finish_reason)
    data, warnings, parse_error = parsed.data, parsed.warnings, parsed.parse_error

    if parse_error is not None and prepared.llm_call.provider in (
        "openai",
        "openai_responses",
        "openai_compatible",
        "openai_responses_compatible",
    ):
        try:
            repair = build_repair_prompt_for_task("outline_generate", raw_output=raw_output)
            if repair is None:
                raise AppError(code="OUTLINE_FIX_UNSUPPORTED", message="该任务不支持输出修复", status_code=400)
            fix_system, fix_user, fix_run_type = repair
            fix_call = with_param_overrides(prepared.llm_call, {"temperature": 0, "max_tokens": 1024})
            fixed = call_llm_and_record(
                logger=logger,
                request_id=request_id,
                actor_user_id=user_id,
                project_id=project_id,
                chapter_id=None,
                run_type=fix_run_type,
                api_key=str(prepared.resolved_api_key),
                prompt_system=fix_system,
                prompt_user=fix_user,
                llm_call=fix_call,
                run_params_extra_json=prepared.run_params_extra_json,
            )
            fixed_parsed = contract.parse(fixed.text)
            fixed_data, fixed_warnings, fixed_error = (
                fixed_parsed.data,
                fixed_parsed.warnings,
                fixed_parsed.parse_error,
            )
            if fixed_error is None and fixed_data.get("chapters"):
                fixed_data["raw_output"] = raw_output
                fixed_data["fixed_json"] = fixed_data.get("raw_json") or fixed.text
                data = fixed_data
                warnings.extend(["json_fixed_via_llm", *fixed_warnings])
                parse_error = None
        except AppError:
            warnings.append("outline_fix_json_failed")

    if parse_error is None:
        data, coverage_warnings = _enforce_outline_chapter_coverage(
            data=data,
            target_chapter_count=prepared.target_chapter_count,
        )
        warnings.extend(coverage_warnings)
        data, fill_warnings, fill_run_ids = _fill_outline_missing_chapters_with_llm(
            data=data,
            target_chapter_count=prepared.target_chapter_count,
            request_id=request_id,
            actor_user_id=user_id,
            project_id=project_id,
            api_key=str(prepared.resolved_api_key),
            llm_call=prepared.llm_call,
            run_params_extra_json=prepared.run_params_extra_json,
        )
        warnings.extend(fill_warnings)
        if fill_run_ids:
            coverage = data.get("chapter_coverage")
            if isinstance(coverage, dict):
                coverage["fill_run_ids"] = fill_run_ids
                data["chapter_coverage"] = coverage

    warnings = _dedupe_warnings(warnings)
    if warnings:
        data["warnings"] = warnings
    if parse_error is not None:
        data["parse_error"] = parse_error
    data["generation_run_id"] = llm_result.run_id
    data["latency_ms"] = llm_result.latency_ms
    if llm_result.dropped_params:
        data["dropped_params"] = llm_result.dropped_params
    if finish_reason is not None:
        data["finish_reason"] = finish_reason
    return data

def prepare_outline_stream_request(
    *,
    project_id: str,
    body: OutlineGenerateRequest,
    user_id: str,
    request_id: str,
    x_llm_provider: str | None,
    x_llm_api_key: str | None,
) -> PreparedOutlineGeneration:
    return prepare_outline_generation(
        project_id=project_id,
        body=body,
        user_id=user_id,
        request_id=request_id,
        x_llm_provider=x_llm_provider,
        x_llm_api_key=x_llm_api_key,
    )
