from __future__ import annotations

import logging

from app.core.errors import AppError
from app.services.generation_service import call_llm_and_record, with_param_overrides
from app.services.outline_generation.chapter_ops import (
    _chapter_score,
    _clone_outline_chapters,
    _collect_missing_chapter_numbers,
    _dedupe_warnings,
    _enforce_outline_chapter_coverage,
    _extract_outline_chapter_numbers,
    _format_chapter_number_ranges,
    _normalize_outline_chapters,
)
from app.services.outline_generation.gap_repair_final_sweep_service import (
    _repair_outline_remaining_gaps_final_sweep_with_llm,
)
from app.services.outline_generation.models import OutlineFillProgressHook
from app.services.outline_generation.policy import (
    OUTLINE_GAP_REPAIR_BATCH_SIZE,
    OUTLINE_GAP_REPAIR_MAX_MISSING,
    OUTLINE_GAP_REPAIR_STAGNANT_LIMIT,
    _outline_gap_repair_max_attempts,
    _recommend_outline_segment_max_tokens,
)
from app.services.outline_generation.prompt_builder import (
    _build_outline_gap_repair_prompts,
    _build_outline_stream_raw_preview,
)
from app.services.output_contracts import contract_for_task

logger = logging.getLogger("ainovel")


def _repair_outline_remaining_gaps_with_llm(
    *,
    data: dict[str, object],
    target_chapter_count: int | None,
    request_id: str,
    actor_user_id: str,
    project_id: str,
    api_key: str,
    llm_call,
    run_params_extra_json: dict[str, object] | None,
    progress_hook: OutlineFillProgressHook | None = None,
) -> tuple[dict[str, object], list[str], list[str]]:
    if not target_chapter_count or target_chapter_count <= 0:
        return data, [], []
    chapters_now, normalize_warnings = _normalize_outline_chapters(data.get("chapters"))
    if not chapters_now:
        return data, normalize_warnings, []

    missing_numbers = _collect_missing_chapter_numbers(
        chapters_now, target_chapter_count=target_chapter_count
    )
    if not missing_numbers:
        return data, [], []

    warnings: list[str] = list(normalize_warnings)
    run_ids: list[str] = []
    if len(missing_numbers) > OUTLINE_GAP_REPAIR_MAX_MISSING:
        warnings.append("outline_gap_repair_skipped_too_many_missing")
        return data, _dedupe_warnings(warnings), run_ids

    max_attempts = _outline_gap_repair_max_attempts(len(missing_numbers))
    contract = contract_for_task("outline_generate")
    attempt = 0
    stagnant_rounds = 0
    last_failure_reason: str | None = None
    last_output_numbers: list[int] | None = None

    if progress_hook is not None:
        progress_hook(
            {
                "event": "gap_repair_start",
                "attempt": 0,
                "max_attempts": max_attempts,
                "remaining_count": len(missing_numbers),
            }
        )

    while attempt < max_attempts:
        missing_numbers = _collect_missing_chapter_numbers(
            chapters_now, target_chapter_count=target_chapter_count
        )
        if not missing_numbers:
            break
        attempt += 1
        batch_missing = missing_numbers[: OUTLINE_GAP_REPAIR_BATCH_SIZE]
        if progress_hook is not None:
            progress_hook(
                {
                    "event": "gap_repair_attempt_start",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "batch_size": len(batch_missing),
                    "remaining_count": len(missing_numbers),
                    "range": _format_chapter_number_ranges(batch_missing),
                }
            )

        repair_system, repair_user = _build_outline_gap_repair_prompts(
            target_chapter_count=target_chapter_count,
            batch_missing=batch_missing,
            existing_chapters=chapters_now,
            outline_md=str(data.get("outline_md") or ""),
            attempt=attempt,
            max_attempts=max_attempts,
            previous_output_numbers=last_output_numbers,
            previous_failure_reason=last_failure_reason,
        )

        current_max_tokens = llm_call.params.get("max_tokens")
        current_max_tokens_int = int(current_max_tokens) if isinstance(current_max_tokens, int) else None
        repair_max_tokens = _recommend_outline_segment_max_tokens(
            requested_count=len(batch_missing),
            provider=llm_call.provider,
            model=llm_call.model,
            current_max_tokens=current_max_tokens_int,
        )
        repair_call = with_param_overrides(llm_call, {"max_tokens": repair_max_tokens}) if repair_max_tokens else llm_call
        repair_extra = dict(run_params_extra_json or {})
        repair_extra["outline_gap_repair"] = {
            "attempt": attempt,
            "max_attempts": max_attempts,
            "target_chapter_count": target_chapter_count,
            "batch_missing": batch_missing,
        }
        try:
            repaired = call_llm_and_record(
                logger=logger,
                request_id=request_id,
                actor_user_id=actor_user_id,
                project_id=project_id,
                chapter_id=None,
                run_type="outline_gap_repair",
                api_key=api_key,
                prompt_system=repair_system,
                prompt_user=repair_user,
                llm_call=repair_call,
                run_params_extra_json=repair_extra,
            )
        except AppError as exc:
            warnings.append("outline_gap_repair_call_failed")
            if exc.code == "LLM_TIMEOUT":
                warnings.append("outline_gap_repair_timeout")
            last_failure_reason = f"模型调用失败（{exc.code}）"
            last_output_numbers = None
            stagnant_rounds += 1
            if progress_hook is not None:
                progress_hook(
                    {
                        "event": "gap_repair_call_failed",
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "remaining_count": len(missing_numbers),
                    }
                )
            if stagnant_rounds >= OUTLINE_GAP_REPAIR_STAGNANT_LIMIT:
                break
            continue

        run_ids.append(repaired.run_id)
        raw_preview = _build_outline_stream_raw_preview(repaired.text)
        raw_chars = len(repaired.text or "")
        repaired_parsed = contract.parse(repaired.text, finish_reason=repaired.finish_reason)
        repaired_data, repaired_warnings, repaired_error = (
            repaired_parsed.data,
            repaired_parsed.warnings,
            repaired_parsed.parse_error,
        )
        warnings.extend(repaired_warnings)
        if repaired_error is not None:
            warnings.append("outline_gap_repair_parse_failed")
            last_failure_reason = str(repaired_error.get("message") or "输出解析失败")
            last_output_numbers = None
            stagnant_rounds += 1
            if progress_hook is not None:
                progress_hook(
                    {
                        "event": "gap_repair_parse_failed",
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "remaining_count": len(missing_numbers),
                        "raw_output_preview": raw_preview,
                        "raw_output_chars": raw_chars,
                    }
                )
            if stagnant_rounds >= OUTLINE_GAP_REPAIR_STAGNANT_LIMIT:
                break
            continue

        incoming, incoming_warnings = _normalize_outline_chapters(repaired_data.get("chapters"))
        warnings.extend(incoming_warnings)
        incoming_numbers = _extract_outline_chapter_numbers(incoming, limit=120)
        if not incoming:
            warnings.append("outline_gap_repair_empty")
            last_failure_reason = "未输出可识别章节"
            last_output_numbers = incoming_numbers
            stagnant_rounds += 1
            if stagnant_rounds >= OUTLINE_GAP_REPAIR_STAGNANT_LIMIT:
                break
            continue

        accepted = 0
        accepted_numbers: list[int] = []
        allowed = set(batch_missing)
        by_number = {
            int(c["number"]): c for c in chapters_now if int(c["number"]) <= target_chapter_count
        }
        for chapter in incoming:
            number = int(chapter["number"])
            if number not in allowed:
                continue
            previous = by_number.get(number)
            if previous is None:
                by_number[number] = chapter
                accepted += 1
                accepted_numbers.append(number)
                continue
            if _chapter_score(chapter) > _chapter_score(previous):
                by_number[number] = chapter

        if accepted <= 0:
            warnings.append("outline_gap_repair_no_progress")
            last_output_numbers = incoming_numbers
            if incoming_numbers:
                last_failure_reason = "输出章号与缺失章号不一致"
            else:
                last_failure_reason = "未输出可采纳章节"
            stagnant_rounds += 1
            if progress_hook is not None:
                progress_hook(
                    {
                        "event": "gap_repair_no_progress",
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "remaining_count": len(missing_numbers),
                        "raw_output_preview": raw_preview,
                        "raw_output_chars": raw_chars,
                        "incoming_numbers_text": _format_chapter_number_ranges(incoming_numbers),
                    }
                )
            if stagnant_rounds >= OUTLINE_GAP_REPAIR_STAGNANT_LIMIT:
                break
            continue

        warnings.append("outline_gap_repair_applied")
        stagnant_rounds = 0
        last_failure_reason = None
        last_output_numbers = None
        chapters_now = [by_number[n] for n in sorted(by_number.keys())]
        remaining = len(
            _collect_missing_chapter_numbers(
                chapters_now, target_chapter_count=target_chapter_count
            )
        )
        if progress_hook is not None:
            progress_hook(
                {
                    "event": "gap_repair_applied",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "accepted": accepted,
                    "accepted_numbers": accepted_numbers,
                    "chapters_snapshot": _clone_outline_chapters(chapters_now),
                    "chapter_count": len(chapters_now),
                    "remaining_count": remaining,
                    "raw_output_preview": raw_preview,
                    "raw_output_chars": raw_chars,
                }
            )

    data["chapters"] = chapters_now
    data, coverage_warnings = _enforce_outline_chapter_coverage(
        data=data, target_chapter_count=target_chapter_count
    )
    warnings.extend(coverage_warnings)
    coverage = data.get("chapter_coverage")
    remaining_count = int(coverage.get("missing_count") or 0) if isinstance(coverage, dict) else 0
    if remaining_count > 0:
        warnings.append("outline_gap_repair_remaining")
        chapters_now, final_warnings, final_run_ids = _repair_outline_remaining_gaps_final_sweep_with_llm(
            chapters_now=chapters_now,
            outline_md=str(data.get("outline_md") or ""),
            target_chapter_count=target_chapter_count,
            request_id=request_id,
            actor_user_id=actor_user_id,
            project_id=project_id,
            api_key=api_key,
            llm_call=llm_call,
            run_params_extra_json=run_params_extra_json,
            progress_hook=progress_hook,
        )
        warnings.extend(final_warnings)
        for run_id in final_run_ids:
            if run_id not in run_ids:
                run_ids.append(run_id)
        data["chapters"] = chapters_now
        data, final_coverage_warnings = _enforce_outline_chapter_coverage(
            data=data, target_chapter_count=target_chapter_count
        )
        warnings.extend(final_coverage_warnings)
        coverage = data.get("chapter_coverage")
        remaining_count = int(coverage.get("missing_count") or 0) if isinstance(coverage, dict) else 0
        if remaining_count > 0:
            warnings.append("outline_gap_repair_final_sweep_remaining")
        else:
            warnings.extend(["outline_gap_repair_final_sweep_resolved", "outline_gap_repair_resolved"])
    else:
        warnings.append("outline_gap_repair_resolved")

    if progress_hook is not None:
        progress_hook(
            {
                "event": "gap_repair_done",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "remaining_count": remaining_count,
            }
        )
    return data, _dedupe_warnings(warnings), run_ids
