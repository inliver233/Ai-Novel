from __future__ import annotations

import logging

from app.core.errors import AppError
from app.services.generation_service import PreparedLlmCall, call_llm_and_record, with_param_overrides
from app.services.outline_generation.chapter_ops import (
    _clone_outline_chapters,
    _dedupe_warnings,
    _enforce_outline_chapter_coverage,
    _extract_outline_chapter_numbers,
    _format_chapter_number_ranges,
    _merge_segment_chapters,
)
from app.services.outline_generation.fill_service import _fill_outline_missing_chapters_with_llm
from app.services.outline_generation.models import OutlineSegmentGenerationResult, OutlineSegmentProgressHook
from app.services.outline_generation.policy import (
    OUTLINE_SEGMENT_STAGNANT_ATTEMPTS_LIMIT,
    _outline_segment_batch_size_for_target,
    _outline_segment_batches,
    _outline_segment_max_attempts_for_batch,
    _recommend_outline_segment_max_tokens,
)
from app.services.outline_generation.prompt_builder import (
    _build_outline_segment_prompts,
    _build_outline_stream_raw_preview,
    _parse_outline_batch_output,
)
from app.services.output_contracts import contract_for_task

logger = logging.getLogger("ainovel")


def _generate_outline_segmented_with_llm(
    *,
    request_id: str,
    actor_user_id: str,
    project_id: str,
    api_key: str,
    llm_call: PreparedLlmCall,
    prompt_system: str,
    prompt_user: str,
    target_chapter_count: int,
    run_params_extra_json: dict[str, object] | None,
    progress_hook: OutlineSegmentProgressHook | None = None,
) -> OutlineSegmentGenerationResult:
    warnings: list[str] = ["outline_segment_mode_enabled"]
    run_ids: list[str] = []
    dropped_params: list[str] = []
    finish_reasons: list[str] = []
    latency_ms_total = 0
    outline_md = ""
    chapters_by_number: dict[int, dict[str, object]] = {}
    batch_size = _outline_segment_batch_size_for_target(target_chapter_count)
    batches = _outline_segment_batches(target_chapter_count, batch_size=batch_size)
    batch_count = len(batches)
    parse_error: dict[str, object] | None = None

    def _emit_progress(payload: dict[str, object]) -> None:
        if progress_hook is None:
            return
        try:
            progress_hook(payload)
        except Exception:
            return

    _emit_progress(
        {
            "event": "segment_start",
            "batch_count": batch_count,
            "target_chapter_count": target_chapter_count,
            "completed_count": 0,
            "remaining_count": target_chapter_count,
            "progress_percent": 12,
        }
    )

    for batch_index, batch in enumerate(batches, start=1):
        missing_numbers = [n for n in batch if n not in chapters_by_number]
        if not missing_numbers:
            continue
        max_attempts = _outline_segment_max_attempts_for_batch(len(batch))
        stagnant_attempts = 0
        attempt = 0
        last_failure_reason: str | None = None
        last_output_numbers: list[int] | None = None

        while missing_numbers and attempt < max_attempts:
            attempt += 1
            range_text = _format_chapter_number_ranges(batch)
            _emit_progress(
                {
                    "event": "batch_attempt_start",
                    "batch_index": batch_index,
                    "batch_count": batch_count,
                    "range": range_text,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "target_chapter_count": target_chapter_count,
                    "completed_count": len(chapters_by_number),
                    "remaining_count": target_chapter_count - len(chapters_by_number),
                    "progress_percent": 12 + int((batch_index - 1) / max(1, batch_count) * 70),
                }
            )
            existing = [chapters_by_number[n] for n in sorted(chapters_by_number.keys())]
            segment_system, segment_user = _build_outline_segment_prompts(
                base_prompt_system=prompt_system,
                base_prompt_user=prompt_user,
                target_chapter_count=target_chapter_count,
                batch_numbers=missing_numbers,
                existing_chapters=existing,
                existing_outline_md=outline_md,
                attempt=attempt,
                max_attempts=max_attempts,
                previous_output_numbers=last_output_numbers,
                previous_failure_reason=last_failure_reason,
            )

            current_max_tokens = llm_call.params.get("max_tokens")
            current_max_tokens_int = int(current_max_tokens) if isinstance(current_max_tokens, int) else None
            segment_max_tokens = _recommend_outline_segment_max_tokens(
                requested_count=len(missing_numbers),
                provider=llm_call.provider,
                model=llm_call.model,
                current_max_tokens=current_max_tokens_int,
            )
            segment_call = with_param_overrides(llm_call, {"max_tokens": segment_max_tokens}) if segment_max_tokens else llm_call

            segment_extra = dict(run_params_extra_json or {})
            segment_extra["outline_segment"] = {
                "batch_index": batch_index,
                "batch_count": batch_count,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "target_chapter_count": target_chapter_count,
                "batch_numbers": missing_numbers,
            }
            try:
                segment_res = call_llm_and_record(
                    logger=logger,
                    request_id=request_id,
                    actor_user_id=actor_user_id,
                    project_id=project_id,
                    chapter_id=None,
                    run_type="outline_segment",
                    api_key=api_key,
                    prompt_system=segment_system,
                    prompt_user=segment_user,
                    llm_call=segment_call,
                    run_params_extra_json=segment_extra,
                )
            except AppError as exc:
                warnings.append("outline_segment_call_failed")
                if exc.code == "LLM_TIMEOUT":
                    warnings.append("outline_segment_timeout")
                last_failure_reason = f"模型调用失败（{exc.code}）"
                last_output_numbers = None
                stagnant_attempts += 1
                _emit_progress(
                    {
                        "event": "batch_call_failed",
                        "batch_index": batch_index,
                        "batch_count": batch_count,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "target_chapter_count": target_chapter_count,
                        "completed_count": len(chapters_by_number),
                        "remaining_count": target_chapter_count - len(chapters_by_number),
                        "failure_reason": last_failure_reason,
                        "progress_percent": 12
                        + int(min(1.0, len(chapters_by_number) / max(1, target_chapter_count)) * 70),
                    }
                )
                if stagnant_attempts >= OUTLINE_SEGMENT_STAGNANT_ATTEMPTS_LIMIT:
                    break
                continue

            if segment_res.run_id not in run_ids:
                run_ids.append(segment_res.run_id)
            latency_ms_total += int(segment_res.latency_ms or 0)
            if segment_res.finish_reason is not None:
                finish_reasons.append(segment_res.finish_reason)
            for item in segment_res.dropped_params:
                if item not in dropped_params:
                    dropped_params.append(item)
            segment_raw_preview = _build_outline_stream_raw_preview(segment_res.text)
            segment_raw_chars = len(segment_res.text or "")

            parsed_data, parsed_warnings, parsed_error = _parse_outline_batch_output(
                text=segment_res.text,
                finish_reason=segment_res.finish_reason,
                fallback_outline_md=outline_md,
            )
            warnings.extend(parsed_warnings)
            if parsed_error is not None:
                warnings.append("outline_segment_parse_failed")
                if segment_res.finish_reason == "length":
                    warnings.append("outline_segment_truncated")
                last_failure_reason = str(parsed_error.get("message") or "输出解析失败")
                last_output_numbers = None
                _emit_progress(
                    {
                        "event": "batch_parse_failed",
                        "batch_index": batch_index,
                        "batch_count": batch_count,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "range": range_text,
                        "target_chapter_count": target_chapter_count,
                        "completed_count": len(chapters_by_number),
                        "remaining_count": target_chapter_count - len(chapters_by_number),
                        "raw_output_preview": segment_raw_preview,
                        "raw_output_chars": segment_raw_chars,
                        "failure_reason": last_failure_reason,
                        "progress_percent": 12
                        + int(min(1.0, len(chapters_by_number) / max(1, target_chapter_count)) * 70),
                    }
                )
                stagnant_attempts += 1
                if stagnant_attempts >= OUTLINE_SEGMENT_STAGNANT_ATTEMPTS_LIMIT:
                    break
                continue

            parsed_outline_md = str(parsed_data.get("outline_md") or "").strip()
            if parsed_outline_md and not outline_md:
                outline_md = parsed_outline_md

            incoming = parsed_data.get("chapters")
            incoming_chapters = incoming if isinstance(incoming, list) else []
            incoming_numbers = _extract_outline_chapter_numbers(incoming_chapters, limit=120)
            accepted, accepted_numbers = _merge_segment_chapters(
                by_number=chapters_by_number,
                incoming=incoming_chapters,
                allowed_numbers=set(missing_numbers),
            )
            if accepted <= 0:
                warnings.append("outline_segment_no_progress")
                missing_set = set(missing_numbers)
                overlap_numbers = [n for n in incoming_numbers if n in missing_set]
                if incoming_numbers and not overlap_numbers:
                    last_failure_reason = "输出章号与当前批次不匹配（疑似重复旧章节）"
                elif incoming_numbers:
                    last_failure_reason = "输出章号包含目标范围，但未形成可采纳新章节"
                else:
                    last_failure_reason = "未输出可识别章节"
                last_output_numbers = incoming_numbers
                _emit_progress(
                    {
                        "event": "batch_no_progress",
                        "batch_index": batch_index,
                        "batch_count": batch_count,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "range": range_text,
                        "incoming_numbers": incoming_numbers,
                        "incoming_numbers_text": _format_chapter_number_ranges(incoming_numbers),
                        "target_chapter_count": target_chapter_count,
                        "completed_count": len(chapters_by_number),
                        "remaining_count": target_chapter_count - len(chapters_by_number),
                        "raw_output_preview": segment_raw_preview,
                        "raw_output_chars": segment_raw_chars,
                        "failure_reason": last_failure_reason,
                        "progress_percent": 12
                        + int(min(1.0, len(chapters_by_number) / max(1, target_chapter_count)) * 70),
                    }
                )
                stagnant_attempts += 1
                if stagnant_attempts >= OUTLINE_SEGMENT_STAGNANT_ATTEMPTS_LIMIT:
                    break
                continue

            warnings.append("outline_segment_applied")
            last_failure_reason = None
            last_output_numbers = None
            stagnant_attempts = 0
            missing_numbers = [n for n in batch if n not in chapters_by_number]
            chapters_snapshot = _clone_outline_chapters(
                [chapters_by_number[n] for n in sorted(chapters_by_number.keys())]
            )
            _emit_progress(
                {
                    "event": "batch_applied",
                    "batch_index": batch_index,
                    "batch_count": batch_count,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "range": range_text,
                    "accepted": accepted,
                    "accepted_numbers": accepted_numbers,
                    "chapters_snapshot": chapters_snapshot,
                    "outline_md": outline_md,
                    "target_chapter_count": target_chapter_count,
                    "completed_count": len(chapters_snapshot),
                    "remaining_count": target_chapter_count - len(chapters_snapshot),
                    "raw_output_preview": segment_raw_preview,
                    "raw_output_chars": segment_raw_chars,
                    "progress_percent": 12
                    + int(min(1.0, len(chapters_snapshot) / max(1, target_chapter_count)) * 80),
                }
            )

        if missing_numbers:
            warnings.append("outline_segment_batch_incomplete")
            chapters_snapshot = _clone_outline_chapters(
                [chapters_by_number[n] for n in sorted(chapters_by_number.keys())]
            )
            _emit_progress(
                {
                    "event": "batch_incomplete",
                    "batch_index": batch_index,
                    "batch_count": batch_count,
                    "range": _format_chapter_number_ranges(batch),
                    "target_chapter_count": target_chapter_count,
                    "completed_count": len(chapters_snapshot),
                    "remaining_count": target_chapter_count - len(chapters_snapshot),
                    "progress_percent": 90,
                }
            )

    chapters_now = [chapters_by_number[n] for n in sorted(chapters_by_number.keys())]
    if not outline_md:
        outline_md = "## AI 大纲\n\n- 分段生成完成，请按需要补充总纲摘要。"
        warnings.append("outline_segment_outline_md_fallback")
    data: dict[str, object] = {"outline_md": outline_md, "chapters": chapters_now}
    data, coverage_warnings = _enforce_outline_chapter_coverage(
        data=data, target_chapter_count=target_chapter_count
    )
    warnings.extend(coverage_warnings)

    def _forward_fill_progress(update: dict[str, object]) -> None:
        if not isinstance(update, dict):
            return
        mapped = dict(update)
        mapped["event"] = f"fill_{mapped.get('event')}"
        mapped["target_chapter_count"] = target_chapter_count
        chapters_snapshot = mapped.get("chapters_snapshot")
        if isinstance(chapters_snapshot, list):
            mapped["completed_count"] = len(chapters_snapshot)
        else:
            chapter_count_raw = mapped.get("chapter_count")
            if isinstance(chapter_count_raw, int):
                mapped["completed_count"] = chapter_count_raw
            else:
                mapped["completed_count"] = len(data.get("chapters") or [])
        mapped["progress_percent"] = 94
        _emit_progress(mapped)

    data, fill_warnings, fill_run_ids = _fill_outline_missing_chapters_with_llm(
        data=data,
        target_chapter_count=target_chapter_count,
        request_id=request_id,
        actor_user_id=actor_user_id,
        project_id=project_id,
        api_key=api_key,
        llm_call=llm_call,
        run_params_extra_json=run_params_extra_json,
        progress_hook=_forward_fill_progress if progress_hook is not None else None,
    )
    warnings.extend(fill_warnings)
    for rid in fill_run_ids:
        if rid not in run_ids:
            run_ids.append(rid)

    chapters_final = data.get("chapters")
    chapters_final_count = len(chapters_final) if isinstance(chapters_final, list) else 0
    if chapters_final_count <= 0:
        parse_error = {"code": "OUTLINE_PARSE_ERROR", "message": "分段生成未得到可用章节结构"}

    coverage = data.get("chapter_coverage")
    if isinstance(coverage, dict):
        coverage["segment_batch_size"] = batch_size
        coverage["segment_batch_count"] = batch_count
        coverage["segment_run_ids"] = run_ids
        data["chapter_coverage"] = coverage

    _emit_progress(
        {
            "event": "segment_done",
            "batch_count": batch_count,
            "target_chapter_count": target_chapter_count,
            "completed_count": chapters_final_count,
            "remaining_count": max(0, target_chapter_count - chapters_final_count),
            "progress_percent": 98,
        }
    )

    meta: dict[str, object] = {
        "mode": "segmented",
        "target_chapter_count": target_chapter_count,
        "batch_size": batch_size,
        "batch_count": batch_count,
        "run_count": len(run_ids),
    }
    return OutlineSegmentGenerationResult(
        data=data,
        warnings=_dedupe_warnings(warnings),
        parse_error=parse_error,
        run_ids=run_ids,
        latency_ms=latency_ms_total,
        dropped_params=dropped_params,
        finish_reasons=finish_reasons,
        meta=meta,
    )
