from __future__ import annotations

import logging

from app.core.errors import AppError
from app.services.generation_service import call_llm_and_record, with_param_overrides
from app.services.outline_generation.chapter_ops import (
    _chapter_score,
    _clone_outline_chapters,
    _collect_missing_chapter_numbers,
    _dedupe_warnings,
    _extract_outline_chapter_numbers,
    _normalize_outline_chapters,
)
from app.services.outline_generation.models import OutlineFillProgressHook
from app.services.outline_generation.policy import (
    OUTLINE_GAP_REPAIR_FINAL_SWEEP_ATTEMPTS_PER_CHAPTER,
    OUTLINE_GAP_REPAIR_FINAL_SWEEP_MAX_MISSING,
    _recommend_outline_segment_max_tokens,
)
from app.services.outline_generation.prompt_builder import (
    _build_outline_gap_repair_prompts,
    _build_outline_stream_raw_preview,
)
from app.services.output_contracts import contract_for_task

logger = logging.getLogger("ainovel")


def _repair_outline_remaining_gaps_final_sweep_with_llm(
    *,
    chapters_now: list[dict[str, object]],
    outline_md: str,
    target_chapter_count: int,
    request_id: str,
    actor_user_id: str,
    project_id: str,
    api_key: str,
    llm_call,
    run_params_extra_json: dict[str, object] | None,
    progress_hook: OutlineFillProgressHook | None = None,
) -> tuple[list[dict[str, object]], list[str], list[str]]:
    warnings: list[str] = []
    run_ids: list[str] = []
    missing_numbers = _collect_missing_chapter_numbers(
        chapters_now, target_chapter_count=target_chapter_count
    )
    if not missing_numbers:
        return chapters_now, warnings, run_ids
    if len(missing_numbers) > OUTLINE_GAP_REPAIR_FINAL_SWEEP_MAX_MISSING:
        warnings.append("outline_gap_repair_final_sweep_skipped_too_many_missing")
        return chapters_now, warnings, run_ids

    warnings.append("outline_gap_repair_final_sweep_started")
    max_attempts = OUTLINE_GAP_REPAIR_FINAL_SWEEP_ATTEMPTS_PER_CHAPTER
    contract = contract_for_task("outline_generate")
    if progress_hook is not None:
        progress_hook(
            {
                "event": "gap_repair_final_sweep_start",
                "attempt": 0,
                "max_attempts": max_attempts,
                "remaining_count": len(missing_numbers),
            }
        )

    for number in list(missing_numbers):
        chapter_fixed = False
        last_failure_reason: str | None = None
        last_output_numbers: list[int] | None = None
        for attempt in range(1, max_attempts + 1):
            remaining_before = len(
                _collect_missing_chapter_numbers(
                    chapters_now, target_chapter_count=target_chapter_count
                )
            )
            if progress_hook is not None:
                progress_hook(
                    {
                        "event": "gap_repair_final_sweep_attempt_start",
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "remaining_count": remaining_before,
                        "range": str(number),
                    }
                )

            repair_system, repair_user = _build_outline_gap_repair_prompts(
                target_chapter_count=target_chapter_count,
                batch_missing=[number],
                existing_chapters=chapters_now,
                outline_md=outline_md,
                attempt=attempt,
                max_attempts=max_attempts,
                previous_output_numbers=last_output_numbers,
                previous_failure_reason=last_failure_reason,
            )
            current_max_tokens = llm_call.params.get("max_tokens")
            current_max_tokens_int = int(current_max_tokens) if isinstance(current_max_tokens, int) else None
            repair_max_tokens = _recommend_outline_segment_max_tokens(
                requested_count=1,
                provider=llm_call.provider,
                model=llm_call.model,
                current_max_tokens=current_max_tokens_int,
            )
            repair_call = with_param_overrides(llm_call, {"max_tokens": repair_max_tokens}) if repair_max_tokens else llm_call
            repair_extra = dict(run_params_extra_json or {})
            repair_extra["outline_gap_repair_final_sweep"] = {
                "attempt": attempt,
                "max_attempts": max_attempts,
                "target_chapter_count": target_chapter_count,
                "chapter_number": number,
            }
            try:
                repaired = call_llm_and_record(
                    logger=logger,
                    request_id=request_id,
                    actor_user_id=actor_user_id,
                    project_id=project_id,
                    chapter_id=None,
                    run_type="outline_gap_repair_final_sweep",
                    api_key=api_key,
                    prompt_system=repair_system,
                    prompt_user=repair_user,
                    llm_call=repair_call,
                    run_params_extra_json=repair_extra,
                )
            except AppError as exc:
                warnings.append("outline_gap_repair_final_sweep_call_failed")
                if exc.code == "LLM_TIMEOUT":
                    warnings.append("outline_gap_repair_final_sweep_timeout")
                last_failure_reason = f"模型调用失败（{exc.code}）"
                last_output_numbers = None
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
                warnings.append("outline_gap_repair_final_sweep_parse_failed")
                last_failure_reason = str(repaired_error.get("message") or "输出解析失败")
                last_output_numbers = None
                continue

            incoming, incoming_warnings = _normalize_outline_chapters(repaired_data.get("chapters"))
            warnings.extend(incoming_warnings)
            incoming_numbers = _extract_outline_chapter_numbers(incoming, limit=120)
            if not incoming:
                warnings.append("outline_gap_repair_final_sweep_empty")
                last_failure_reason = "未输出可识别章节"
                last_output_numbers = incoming_numbers
                continue

            by_number = {
                int(c["number"]): c for c in chapters_now if int(c["number"]) <= target_chapter_count
            }
            accepted = 0
            accepted_numbers: list[int] = []
            for chapter in incoming:
                chapter_number = int(chapter["number"])
                if chapter_number != number:
                    continue
                previous = by_number.get(chapter_number)
                if previous is None:
                    by_number[chapter_number] = chapter
                    accepted += 1
                    accepted_numbers.append(chapter_number)
                    continue
                if _chapter_score(chapter) > _chapter_score(previous):
                    by_number[chapter_number] = chapter

            if accepted <= 0:
                warnings.append("outline_gap_repair_final_sweep_no_progress")
                if incoming_numbers:
                    last_failure_reason = "输出章号与目标章号不一致"
                else:
                    last_failure_reason = "未输出可采纳章节"
                last_output_numbers = incoming_numbers
                continue

            warnings.append("outline_gap_repair_final_sweep_applied")
            last_failure_reason = None
            last_output_numbers = None
            chapters_now = [by_number[n] for n in sorted(by_number.keys())]
            chapter_fixed = True
            remaining_after = len(
                _collect_missing_chapter_numbers(
                    chapters_now, target_chapter_count=target_chapter_count
                )
            )
            if progress_hook is not None:
                progress_hook(
                    {
                        "event": "gap_repair_final_sweep_applied",
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "accepted": accepted,
                        "accepted_numbers": accepted_numbers,
                        "chapters_snapshot": _clone_outline_chapters(chapters_now),
                        "chapter_count": len(chapters_now),
                        "remaining_count": remaining_after,
                        "raw_output_preview": raw_preview,
                        "raw_output_chars": raw_chars,
                    }
                )
            break

        if not chapter_fixed:
            warnings.append("outline_gap_repair_final_sweep_chapter_unresolved")

    remaining_final = len(
        _collect_missing_chapter_numbers(chapters_now, target_chapter_count=target_chapter_count)
    )
    if progress_hook is not None:
        progress_hook(
            {
                "event": "gap_repair_final_sweep_done",
                "attempt": max_attempts,
                "max_attempts": max_attempts,
                "remaining_count": remaining_final,
            }
        )
    return chapters_now, _dedupe_warnings(warnings), run_ids
