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
    _normalize_outline_chapters,
)
from app.services.outline_generation.gap_repair_service import _repair_outline_remaining_gaps_with_llm
from app.services.outline_generation.models import OutlineFillProgressHook
from app.services.outline_generation.policy import (
    OUTLINE_FILL_STAGNANT_ROUNDS_LIMIT,
    _outline_fill_batch_size_for_missing,
    _outline_fill_max_attempts_for_missing,
    _recommend_outline_max_tokens,
)
from app.services.outline_generation.prompt_builder import (
    _build_outline_missing_chapters_prompts,
    _build_outline_stream_raw_preview,
)
from app.services.output_contracts import contract_for_task

logger = logging.getLogger("ainovel")


def _fill_outline_missing_chapters_with_llm(
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

    warnings: list[str] = list(normalize_warnings)
    continue_run_ids: list[str] = []
    contract = contract_for_task("outline_generate")
    missing_numbers = _collect_missing_chapter_numbers(
        chapters_now, target_chapter_count=target_chapter_count
    )
    max_attempts = _outline_fill_max_attempts_for_missing(len(missing_numbers))
    stagnant_rounds = 0
    attempt = 0

    if progress_hook is not None:
        progress_hook(
            {
                "event": "fill_start",
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
        batch_size = _outline_fill_batch_size_for_missing(len(missing_numbers))
        batch_missing = missing_numbers[:batch_size]
        attempt += 1
        if progress_hook is not None:
            progress_hook(
                {
                    "event": "attempt_start",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "batch_size": len(batch_missing),
                    "remaining_count": len(missing_numbers),
                }
            )
        fill_system, fill_user = _build_outline_missing_chapters_prompts(
            target_chapter_count=target_chapter_count,
            missing_numbers=batch_missing,
            existing_chapters=chapters_now,
            outline_md=str(data.get("outline_md") or ""),
        )
        current_max_tokens = llm_call.params.get("max_tokens")
        current_max_tokens_int = int(current_max_tokens) if isinstance(current_max_tokens, int) else None
        fill_max_tokens = _recommend_outline_max_tokens(
            target_chapter_count=max(41, len(batch_missing) + 20),
            provider=llm_call.provider,
            model=llm_call.model,
            current_max_tokens=current_max_tokens_int,
        )
        fill_call = with_param_overrides(llm_call, {"max_tokens": fill_max_tokens}) if fill_max_tokens else llm_call
        fill_extra = dict(run_params_extra_json or {})
        fill_extra["outline_fill_missing"] = {
            "attempt": attempt,
            "max_attempts": max_attempts,
            "target_chapter_count": target_chapter_count,
            "batch_missing": batch_missing,
        }
        try:
            filled = call_llm_and_record(
                logger=logger,
                request_id=request_id,
                actor_user_id=actor_user_id,
                project_id=project_id,
                chapter_id=None,
                run_type="outline_fill_missing",
                api_key=api_key,
                prompt_system=fill_system,
                prompt_user=fill_user,
                llm_call=fill_call,
                run_params_extra_json=fill_extra,
            )
        except AppError as exc:
            warnings.append("outline_fill_missing_call_failed")
            if exc.code == "LLM_TIMEOUT":
                warnings.append("outline_fill_missing_timeout")
            stagnant_rounds += 1
            if progress_hook is not None:
                progress_hook(
                    {
                        "event": "attempt_call_failed",
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "error_code": exc.code,
                        "remaining_count": len(missing_numbers),
                    }
                )
            if stagnant_rounds >= OUTLINE_FILL_STAGNANT_ROUNDS_LIMIT:
                break
            continue
        continue_run_ids.append(filled.run_id)
        fill_raw_preview = _build_outline_stream_raw_preview(filled.text)
        fill_raw_chars = len(filled.text or "")
        filled_parsed = contract.parse(filled.text, finish_reason=filled.finish_reason)
        filled_data, filled_warnings, filled_error = (
            filled_parsed.data,
            filled_parsed.warnings,
            filled_parsed.parse_error,
        )
        warnings.extend(filled_warnings)
        if filled_error is not None:
            warnings.append("outline_fill_missing_parse_failed")
            if filled.finish_reason == "length":
                warnings.append("outline_fill_missing_truncated")
            stagnant_rounds += 1
            if progress_hook is not None:
                progress_hook(
                    {
                        "event": "attempt_parse_failed",
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "remaining_count": len(missing_numbers),
                        "raw_output_preview": fill_raw_preview,
                        "raw_output_chars": fill_raw_chars,
                    }
                )
            if stagnant_rounds >= OUTLINE_FILL_STAGNANT_ROUNDS_LIMIT:
                break
            continue

        incoming, incoming_warnings = _normalize_outline_chapters(filled_data.get("chapters"))
        warnings.extend(incoming_warnings)
        if not incoming:
            warnings.append("outline_fill_missing_empty")
            stagnant_rounds += 1
            if progress_hook is not None:
                progress_hook(
                    {
                        "event": "attempt_empty",
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "remaining_count": len(missing_numbers),
                    }
                )
            if stagnant_rounds >= OUTLINE_FILL_STAGNANT_ROUNDS_LIMIT:
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
            warnings.append("outline_fill_missing_no_progress")
            stagnant_rounds += 1
            if progress_hook is not None:
                progress_hook(
                    {
                        "event": "attempt_no_progress",
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "remaining_count": len(missing_numbers),
                    }
                )
            if stagnant_rounds >= OUTLINE_FILL_STAGNANT_ROUNDS_LIMIT:
                break
            continue

        warnings.append("outline_fill_missing_applied")
        stagnant_rounds = 0
        chapters_now = [by_number[n] for n in sorted(by_number.keys())]
        remaining = len(
            _collect_missing_chapter_numbers(
                chapters_now, target_chapter_count=target_chapter_count
            )
        )
        if progress_hook is not None:
            chapter_snapshot = _clone_outline_chapters(chapters_now)
            progress_hook(
                {
                    "event": "attempt_applied",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "accepted": accepted,
                    "accepted_numbers": accepted_numbers,
                    "chapters_snapshot": chapter_snapshot,
                    "chapter_count": len(chapter_snapshot),
                    "remaining_count": remaining,
                    "raw_output_preview": fill_raw_preview,
                    "raw_output_chars": fill_raw_chars,
                }
            )

    data["chapters"] = chapters_now
    data, coverage_warnings = _enforce_outline_chapter_coverage(
        data=data, target_chapter_count=target_chapter_count
    )
    warnings.extend(coverage_warnings)
    coverage = data.get("chapter_coverage")
    remaining_count = int(coverage.get("missing_count") or 0) if isinstance(coverage, dict) else 0

    gap_repair_run_ids: list[str] = []
    if remaining_count > 0:
        repaired_data, repair_warnings, repair_run_ids = _repair_outline_remaining_gaps_with_llm(
            data=data,
            target_chapter_count=target_chapter_count,
            request_id=request_id,
            actor_user_id=actor_user_id,
            project_id=project_id,
            api_key=api_key,
            llm_call=llm_call,
            run_params_extra_json=run_params_extra_json,
            progress_hook=progress_hook,
        )
        data = repaired_data
        warnings.extend(repair_warnings)
        gap_repair_run_ids = repair_run_ids
        for run_id in repair_run_ids:
            if run_id not in continue_run_ids:
                continue_run_ids.append(run_id)

    coverage = data.get("chapter_coverage")
    remaining_count = int(coverage.get("missing_count") or 0) if isinstance(coverage, dict) else 0
    if remaining_count > 0:
        warnings.append("outline_fill_missing_remaining")
    if gap_repair_run_ids and isinstance(coverage, dict):
        coverage["gap_repair_run_ids"] = gap_repair_run_ids
        data["chapter_coverage"] = coverage
    if progress_hook is not None:
        progress_hook(
            {
                "event": "fill_done",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "remaining_count": remaining_count,
            }
        )
    return data, _dedupe_warnings(warnings), continue_run_ids
