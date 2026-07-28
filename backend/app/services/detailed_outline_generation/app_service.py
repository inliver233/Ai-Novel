from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.utils import new_id, utc_now
from app.llm.capabilities import max_context_tokens_limit
from app.models.chapter import Chapter
from app.models.detailed_outline import DetailedOutline
from app.models.outline import Outline
from app.models.project import Project
from app.services.chapter_numbering import compute_chapter_offset, extract_positive_chapter_numbers
from app.services.detailed_outline_generation.models import DetailedOutlineResult, VolumeInfo
from app.services.detailed_outline_generation.prepare_service import prepare_detailed_outline_render_values
from app.services.generation_service import PreparedLlmCall, call_llm_and_record, with_param_overrides
from app.services.llm_task_preset_resolver import resolve_task_llm_config
from app.services.output_parsers import extract_json_value, likely_truncated_json
from app.services.prompt_budget import estimate_tokens
from app.services.prompt_presets import render_preset_for_task

logger = logging.getLogger("ainovel")

# ---------------------------------------------------------------------------
# Volume pattern: matches Chinese and English volume headings in markdown.
#   ## 卷一  /  ## 第一卷  /  ## 第1卷  /  ## Volume 1  etc.
# ---------------------------------------------------------------------------
_VOLUME_HEADING_RE = re.compile(
    r"(?m)^##\s+"
    r"(?:"
    r"卷([一二三四五六七八九十百零\d]+)"          # 卷一, 卷1
    r"|第([一二三四五六七八九十百零\d]+)卷"        # 第一卷, 第1卷
    r"|Volume\s*(\d+)"                              # Volume 1
    r")"
    r"[：:\s]*(.*)$",
    re.IGNORECASE,
)

_CN_NUM_MAP: dict[str, int] = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "百": 100,
}


def _cn_to_int(s: str) -> int:
    """Convert a simple Chinese numeral string to int.  Handles 一..十, 十一..九十九."""
    s = s.strip()
    if s.isdigit():
        return int(s)
    # Single character
    if len(s) == 1 and s in _CN_NUM_MAP:
        return _CN_NUM_MAP[s]
    # Two/three character patterns like 十一, 二十, 二十三
    result = 0
    if "百" in s:
        parts = s.split("百")
        hundreds = _CN_NUM_MAP.get(parts[0], 1) if parts[0] else 1
        result += hundreds * 100
        s = parts[1] if len(parts) > 1 else ""
    if "十" in s:
        parts = s.split("十")
        tens = _CN_NUM_MAP.get(parts[0], 1) if parts[0] else 1
        result += tens * 10
        s = parts[1] if len(parts) > 1 else ""
    if s:
        result += _CN_NUM_MAP.get(s, 0)
    return result if result > 0 else 1


# ---------------------------------------------------------------------------
# extract_volumes_from_outline
# ---------------------------------------------------------------------------

def extract_volumes_from_outline(outline: Outline, db: Session) -> list[VolumeInfo]:
    """Extract volume structure from an outline.

    Strategy:
    1. If structure_json has a ``"volumes"`` key -- use it directly.
    2. Parse content_md for volume heading markers.
    3. Fallback: treat the entire outline as a single volume.
    """
    # --- strategy 1: structure_json ---
    structure = _parse_structure_json(outline.structure_json)
    if isinstance(structure, dict) and "volumes" in structure:
        raw_volumes = structure["volumes"]
        if isinstance(raw_volumes, list) and raw_volumes:
            return _volumes_from_structure(raw_volumes)

    # --- strategy 2: parse markdown headings ---
    content = (outline.content_md or "").strip()
    if content:
        volumes = _volumes_from_markdown(content)
        if volumes:
            return volumes

    # --- strategy 3: single-volume fallback ---
    return [
        VolumeInfo(
            number=1,
            title="",
            beats_text=content,
            chapter_range_start=1,
            chapter_range_end=0,  # unknown
        )
    ]


def _parse_structure_json(raw: str | None) -> Any | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _volumes_from_structure(raw_volumes: list) -> list[VolumeInfo]:
    """Build VolumeInfo list from a ``structure_json["volumes"]`` array."""
    result: list[VolumeInfo] = []
    chapter_cursor = 1
    for idx, vol in enumerate(raw_volumes):
        if not isinstance(vol, dict):
            continue
        number = int(vol.get("number", idx + 1))
        title = str(vol.get("title") or "")
        beats_raw = vol.get("summary") or vol.get("beats") or vol.get("content") or vol.get("beats_text") or ""
        if isinstance(beats_raw, list):
            beats_text = "\n".join(str(b) for b in beats_raw)
        else:
            beats_text = str(beats_raw)

        ch_start = int(vol.get("chapter_range_start", chapter_cursor))
        ch_end = int(vol.get("chapter_range_end", 0))
        chapter_count = int(vol.get("chapter_count", 0))
        if ch_end == 0 and chapter_count > 0:
            ch_end = ch_start + chapter_count - 1

        result.append(VolumeInfo(
            number=number,
            title=title,
            beats_text=beats_text,
            chapter_range_start=ch_start,
            chapter_range_end=ch_end,
        ))
        if ch_end > 0:
            chapter_cursor = ch_end + 1
        elif chapter_count > 0:
            chapter_cursor += chapter_count
    return result


def _volumes_from_markdown(content: str) -> list[VolumeInfo]:
    """Parse volume headings from markdown content."""
    matches = list(_VOLUME_HEADING_RE.finditer(content))
    if not matches:
        return []

    volumes: list[VolumeInfo] = []
    for i, m in enumerate(matches):
        # Determine volume number from whichever capture group matched
        num_str = m.group(1) or m.group(2) or m.group(3) or str(i + 1)
        number = _cn_to_int(num_str)
        title = (m.group(4) or "").strip()

        # Extract text between this heading and the next volume heading
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        beats_text = content[start:end].strip()

        volumes.append(VolumeInfo(
            number=number,
            title=title,
            beats_text=beats_text,
            chapter_range_start=0,  # will be calculated later if needed
            chapter_range_end=0,
        ))

    # Assign chapter_range_start sequentially (estimate)
    cursor = 1
    result: list[VolumeInfo] = []
    for vol in volumes:
        result.append(VolumeInfo(
            number=vol.number,
            title=vol.title,
            beats_text=vol.beats_text,
            chapter_range_start=cursor,
            chapter_range_end=0,
        ))
        # Rough estimate: count chapter-like headings inside beats_text
        ch_count = len(re.findall(r"(?m)^###?\s+", vol.beats_text))
        cursor += max(ch_count, 1)
    return result


# ---------------------------------------------------------------------------
# generate_detailed_outline_for_volume
# ---------------------------------------------------------------------------

def generate_detailed_outline_for_volume(
    outline: Outline,
    volume_info: VolumeInfo,
    project: Project,
    llm_config: PreparedLlmCall,
    api_key: str,
    request_id: str,
    user_id: str,
    db: Session,
    *,
    previous_volume_summary: str | None = None,
    next_volume_summary: str | None = None,
    chapters_per_volume: int | None = None,
    instruction: str | None = None,
    context_flags: dict | None = None,
) -> DetailedOutlineResult:
    """Generate a detailed outline for a single volume.

    1. Build render values from project/outline/volume context.
    2. Render prompt preset ``'detailed_outline_generate'``.
    3. Call LLM and record the generation run.
    4. Parse JSON output into structured format.
    5. Create/update ``DetailedOutline`` record.
    6. Return result.
    """
    # 1 -- render values
    render_values = prepare_detailed_outline_render_values(
        outline,
        volume_info,
        project,
        db,
        previous_volume_summary=previous_volume_summary,
        next_volume_summary=next_volume_summary,
        chapters_per_volume=chapters_per_volume,
        instruction=instruction,
        context_flags=context_flags,
    )

    # 2 -- render prompt
    prompt_system, prompt_user, _prompt_messages, _, _, _, render_log = render_preset_for_task(
        db,
        project_id=project.id,
        task="detailed_outline_generate",
        values=render_values,
        macro_seed=request_id,
        provider=llm_config.provider,
    )
    prompt_render_log_json = json.dumps(render_log, ensure_ascii=False)

    # 2.5 -- validate prompt + adjust max_tokens for context safety
    if not prompt_system.strip() and not prompt_user.strip():
        raise AppError(
            code="DETAILED_OUTLINE_EMPTY_PROMPT",
            message="细纲 prompt 渲染为空，请检查 Prompt 预设配置",
            status_code=500,
        )
    prompt_tokens = estimate_tokens(prompt_system) + estimate_tokens(prompt_user)
    ctx_limit = max_context_tokens_limit(llm_config.provider, llm_config.model)
    current_max_tokens = llm_config.params.get("max_tokens")
    if isinstance(ctx_limit, int) and ctx_limit > 0:
        safe_max = max(4096, ctx_limit - prompt_tokens - 512)
        if current_max_tokens is None or (isinstance(current_max_tokens, int) and current_max_tokens > safe_max):
            llm_config = with_param_overrides(llm_config, {"max_tokens": safe_max})
            logger.info(
                "detailed_outline_max_tokens_adjusted prompt_tokens=%d ctx_limit=%d safe_max=%d original=%s",
                prompt_tokens, ctx_limit, safe_max, current_max_tokens,
            )
    elif current_max_tokens is None:
        llm_config = with_param_overrides(llm_config, {"max_tokens": 16000})

    # 3 -- call LLM
    llm_result = call_llm_and_record(
        logger=logger,
        request_id=request_id,
        actor_user_id=user_id,
        project_id=project.id,
        chapter_id=None,
        run_type="detailed_outline_generate",
        api_key=api_key,
        prompt_system=prompt_system,
        prompt_user=prompt_user,
        prompt_render_log_json=prompt_render_log_json,
        llm_call=llm_config,
    )

    # 4 -- parse output
    content_md, structure, warnings, parse_error = _parse_detailed_outline_output(llm_result.text)

    chapter_count = 0
    if structure is not None:
        chapters = structure.get("chapters")
        if isinstance(chapters, list):
            chapter_count = len(chapters)

    # 5 -- persist DetailedOutline
    detailed_outline_id = _upsert_detailed_outline(
        db,
        outline_id=outline.id,
        project_id=project.id,
        volume_number=volume_info.number,
        volume_title=volume_info.title or "",
        content_md=content_md,
        structure=structure,
    )

    # 6 -- return
    return DetailedOutlineResult(
        detailed_outline_id=detailed_outline_id,
        volume_number=volume_info.number,
        volume_title=volume_info.title,
        content_md=content_md,
        structure=structure,
        chapter_count=chapter_count,
        run_id=llm_result.run_id,
        warnings=warnings,
        parse_error=parse_error,
    )


# ---------------------------------------------------------------------------
# generate_all_detailed_outlines  (SSE generator)
# ---------------------------------------------------------------------------

def generate_all_detailed_outlines(
    outline_id: str,
    project_id: str,
    user_id: str,
    request_id: str,
    db: Session,
    *,
    x_llm_api_key: str | None = None,
    chapters_per_volume: int | None = None,
    instruction: str | None = None,
    context_flags: dict | None = None,
) -> Iterator[dict]:
    """Generate detailed outlines for ALL volumes. Yields SSE-style event dicts.

    Event types emitted:
        start, volume_start, volume_progress, volume_complete, complete, error
    """
    # -- load entities --
    outline = db.get(Outline, outline_id)
    if outline is None:
        yield {"type": "error", "message": "Outline not found"}
        return
    project = db.get(Project, project_id)
    if project is None:
        yield {"type": "error", "message": "Project not found"}
        return

    # -- extract volumes --
    volumes = extract_volumes_from_outline(outline, db)
    total_volumes = len(volumes)
    yield {"type": "start", "total_volumes": total_volumes}

    # -- try to create from existing outline structure (no LLM needed) --
    structure = _parse_structure_json(outline.structure_json)
    outline_chapters: list[dict[str, Any]] = []
    if isinstance(structure, dict):
        raw_vols = structure.get("volumes")
        if isinstance(raw_vols, list) and raw_vols:
            # Fast path: volumes with summary = 细纲 content, save directly without LLM
            patched_vols: list[dict[str, Any]] = []
            for _i, _vol in enumerate(raw_vols):
                if not isinstance(_vol, dict):
                    continue
                cp = dict(_vol)
                try:
                    _num = int(cp.get("number", 0))
                except (TypeError, ValueError):
                    _num = 0
                if _num <= 0:
                    cp["number"] = _i + 1
                else:
                    cp["number"] = _num
                cp["title"] = str(cp.get("title") or "")
                cp["summary"] = str(cp.get("summary") or "")
                patched_vols.append(cp)
            outline_volumes = sorted(patched_vols, key=lambda v: int(v.get("number", 0))) if patched_vols else []

            if outline_volumes:
                for vol in outline_volumes:
                    vol_number = int(vol.get("number", 0) or 0)
                    vol_title = str(vol.get("title") or "")
                    vol_summary = str(vol.get("summary") or "")

                    yield {
                        "type": "volume_start",
                        "volume_number": vol_number,
                        "volume_title": vol_title,
                        "total_volumes": total_volumes,
                    }

                    detailed_outline_id = _upsert_detailed_outline(
                        db,
                        outline_id=outline.id,
                        project_id=project_id,
                        volume_number=vol_number,
                        volume_title=vol_title,
                        content_md=vol_summary,
                        structure=None,
                    )

                    yield {
                        "type": "volume_complete",
                        "volume_number": vol_number,
                        "chapter_count": 0,
                        "detailed_outline_id": detailed_outline_id,
                        "total_volumes": total_volumes,
                    }

                yield {
                    "type": "complete",
                    "total_volumes": total_volumes,
                    "total_chapters": 0,
                }
                return
        else:
            raw_ch = structure.get("chapters")
            if isinstance(raw_ch, list) and raw_ch:
                # Accept chapters even without valid number — assign sequential if missing
                patched: list[dict[str, Any]] = []
                for _i, _ch in enumerate(raw_ch):
                    if not isinstance(_ch, dict):
                        continue
                    cp = dict(_ch)
                    try:
                        _num = int(cp.get("number", 0))
                    except (TypeError, ValueError):
                        _num = 0
                    if _num <= 0:
                        cp["number"] = _i + 1
                    patched.append(cp)
                outline_chapters = _normalize_chapters(patched) if patched else []

    if outline_chapters:
        total_chapters = 0
        for idx, vol in enumerate(volumes):
            yield {
                "type": "volume_start",
                "volume_number": vol.number,
                "volume_title": vol.title,
                "total_volumes": total_volumes,
            }

            # Assign chapters to volume (best-effort)
            if total_volumes == 1:
                vol_chapters = outline_chapters
            else:
                ch_end = vol.chapter_range_end if vol.chapter_range_end > 0 else 999999
                vol_chapters = [
                    ch for ch in outline_chapters
                    if vol.chapter_range_start <= int(ch.get("number", 0)) <= ch_end
                ]
                if not vol_chapters:
                    vol_chapters = outline_chapters  # fallback: assign all

            # Build content_md from chapters
            content_parts: list[str] = []
            for ch in vol_chapters:
                ch_num = ch.get("number", "?")
                ch_title = str(ch.get("title", ""))
                ch_summary = str(ch.get("summary", ""))
                beats = ch.get("beats")
                parts: list[str] = []
                if ch_summary:
                    parts.append(ch_summary)
                if isinstance(beats, list) and beats:
                    parts.append("\n".join(f"- {str(b)}" for b in beats if b is not None))
                content_parts.append(f"### {ch_num}. {ch_title}\n" + "\n\n".join(parts))
            content_md = "\n\n".join(content_parts)

            structure_data = {"chapters": _normalize_chapters(vol_chapters)}
            detailed_outline_id = _upsert_detailed_outline(
                db,
                outline_id=outline.id,
                project_id=project_id,
                volume_number=vol.number,
                volume_title=vol.title or "",
                content_md=content_md,
                structure=structure_data,
            )
            chapter_count = len(vol_chapters)
            total_chapters += chapter_count

            yield {
                "type": "volume_complete",
                "volume_number": vol.number,
                "chapter_count": chapter_count,
                "detailed_outline_id": detailed_outline_id,
                "total_volumes": total_volumes,
            }

        yield {
            "type": "complete",
            "total_volumes": total_volumes,
            "total_chapters": total_chapters,
        }
        return  # No LLM generation needed

    # -- resolve LLM config --
    resolved = None
    for task_key in ("detailed_outline_generate", "outline_generate"):
        try:
            resolved = resolve_task_llm_config(
                db,
                project=project,
                user_id=user_id,
                task_key=task_key,
                header_api_key=x_llm_api_key,
            )
        except AppError:
            resolved = None
        if resolved is not None:
            break
    if resolved is None:
        yield {"type": "error", "message": "LLM 配置未找到，请先在 Prompts 页保存 LLM 配置"}
        return
    llm_config = resolved.llm_call
    api_key = str(resolved.api_key)

    # -- generate per volume --
    total_chapters = 0
    results: list[DetailedOutlineResult] = []

    for idx, vol in enumerate(volumes):
        yield {
            "type": "volume_start",
            "volume_number": vol.number,
            "volume_title": vol.title,
            "total_volumes": total_volumes,
        }

        # Build prev/next volume summaries for context continuity
        prev_summary = _get_volume_summary(results, idx - 1) if idx > 0 else None
        next_summary = _get_volume_beats_preview(volumes, idx + 1) if idx + 1 < total_volumes else None

        try:
            result = generate_detailed_outline_for_volume(
                outline,
                vol,
                project,
                llm_config,
                api_key,
                request_id,
                user_id,
                db,
                previous_volume_summary=prev_summary,
                next_volume_summary=next_summary,
                chapters_per_volume=chapters_per_volume,
                instruction=instruction,
                context_flags=context_flags,
            )
            results.append(result)
            total_chapters += result.chapter_count

            yield {
                "type": "volume_complete",
                "volume_number": vol.number,
                "chapter_count": result.chapter_count,
                "detailed_outline_id": result.detailed_outline_id,
                "total_volumes": total_volumes,
            }
        except AppError as exc:
            logger.warning(
                "detailed_outline_volume_error volume=%d code=%s msg=%s",
                vol.number, exc.code, exc.message,
            )
            yield {
                "type": "error",
                "volume_number": vol.number,
                "message": exc.message,
                "code": exc.code,
            }
        except Exception as exc:
            logger.exception("detailed_outline_volume_unexpected_error volume=%d", vol.number)
            yield {
                "type": "error",
                "volume_number": vol.number,
                "message": str(exc),
            }

    yield {
        "type": "complete",
        "total_volumes": total_volumes,
        "total_chapters": total_chapters,
    }


# ---------------------------------------------------------------------------
# create_chapters_from_detailed_outline
# ---------------------------------------------------------------------------

def _format_chapter_numbers(numbers: set[int]) -> str:
    ordered = sorted(numbers)
    if not ordered:
        return ""

    ranges: list[str] = []
    start = ordered[0]
    end = ordered[0]
    for number in ordered[1:]:
        if number == end + 1:
            end = number
            continue
        ranges.append(f"{start}-{end}" if start != end else str(start))
        start = end = number
    ranges.append(f"{start}-{end}" if start != end else str(start))
    return ",".join(ranges)


def _is_contiguous_number_set(numbers: set[int]) -> bool:
    if not numbers:
        return False
    start = min(numbers)
    end = max(numbers)
    return len(numbers) == (end - start + 1)


def create_chapters_from_detailed_outline(
    detailed_outline_id: str,
    db: Session,
    *,
    replace: bool = False,
) -> list[dict]:
    """Create Chapter records from a DetailedOutline's structure_json.

    Chapters are numbered globally across volumes: volume 1 gets 1..N,
    volume 2 gets N+1..N+M, etc.  When ``replace=True`` only the
    chapters that belong to *this* volume's target numbers are deleted.
    """
    detail = db.get(DetailedOutline, detailed_outline_id)
    if detail is None:
        raise AppError.not_found("DetailedOutline not found")

    structure = _parse_structure_json(detail.structure_json)
    if not isinstance(structure, dict):
        raise AppError(
            code="DETAILED_OUTLINE_NO_STRUCTURE",
            message="DetailedOutline has no valid structure_json",
        )

    chapters_raw = structure.get("chapters")
    if not isinstance(chapters_raw, list) or not chapters_raw:
        raise AppError(
            code="DETAILED_OUTLINE_NO_CHAPTERS",
            message="structure_json contains no chapters",
        )

    offset = compute_chapter_offset(db, detail)
    raw_numbers = extract_positive_chapter_numbers(chapters_raw)
    target_numbers = {offset + (i + 1) for i in range(len(raw_numbers))}
    target_numbers_text = _format_chapter_numbers(target_numbers)

    if replace and target_numbers:
        replace_numbers = set(target_numbers)
        has_later_volume = db.execute(
            select(DetailedOutline.id)
            .where(DetailedOutline.outline_id == detail.outline_id)
            .where(DetailedOutline.volume_number > detail.volume_number)
            .limit(1)
        ).scalar_one_or_none()
        if has_later_volume is None and _is_contiguous_number_set(target_numbers):
            replace_numbers.update(
                row[0]
                for row in db.execute(
                    select(Chapter.number)
                    .where(Chapter.outline_id == detail.outline_id)
                    .where(Chapter.project_id == detail.project_id)
                    .where(Chapter.number >= min(target_numbers))
                ).all()
            )
        existing = (
            db.execute(
                select(Chapter)
                .where(Chapter.outline_id == detail.outline_id)
                .where(Chapter.project_id == detail.project_id)
                .where(Chapter.number.in_(replace_numbers))
            )
            .scalars()
            .all()
        )
        for ch in existing:
            db.delete(ch)
        db.flush()

    if not replace and target_numbers:
        conflict_numbers = {
            row[0]
            for row in db.execute(
                select(Chapter.number)
                .where(Chapter.outline_id == detail.outline_id)
                .where(Chapter.project_id == detail.project_id)
                .where(Chapter.number.in_(target_numbers))
            ).all()
        }
        if conflict_numbers:
            raise AppError(
                code="CONFLICT",
                message=(
                    f"第{detail.volume_number}卷已有 {len(conflict_numbers)} 个章节"
                    f"(编号{_format_chapter_numbers(conflict_numbers) or target_numbers_text})，请选择替换"
                ),
                status_code=409,
            )

    # Sort chapters by their raw number to maintain order, then assign sequential local indices
    sorted_chapters = sorted(
        [ch for ch in chapters_raw if isinstance(ch, dict)],
        key=lambda c: int(c.get("number", 0)) if isinstance(c.get("number"), (int, float, str)) else 0,
    )

    created: list[dict] = []
    local_idx = 0
    for ch_raw in sorted_chapters:
        try:
            raw_number = int(ch_raw.get("number", 0))
        except (TypeError, ValueError):
            continue
        if raw_number <= 0:
            continue

        local_idx += 1
        global_number = offset + local_idx

        title = str(ch_raw.get("title") or "")
        summary_text = str(ch_raw.get("summary") or "")
        beats = ch_raw.get("beats") or []
        beats_text = ""
        if isinstance(beats, list) and beats:
            beats_text = "\n".join(f"- {str(b)}" for b in beats if b is not None)

        plan_parts: list[str] = []
        if summary_text.strip():
            plan_parts.append(summary_text.strip())
        if beats_text.strip():
            plan_parts.append(beats_text.strip())
        plan = "\n\n".join(plan_parts) if plan_parts else ""

        chapter = Chapter(
            id=new_id(),
            project_id=detail.project_id,
            outline_id=detail.outline_id,
            number=global_number,
            title=title,
            plan=plan,
            status="planned",
        )
        db.add(chapter)
        created.append({
            "id": chapter.id,
            "number": global_number,
            "title": title,
            "plan": plan,
        })

    db.commit()
    return created


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_detailed_outline_output(
    text: str,
) -> tuple[str, dict[str, Any] | None, list[str], dict[str, Any] | None]:
    """Parse LLM output for detailed outline generation.

    Tries multiple extraction strategies (same approach as outline_payload_normalizer):
    1. Extract JSON from code fence or raw text.
    2. Fallback: use raw text as content_md with no structure.

    Returns (content_md, structure, warnings, parse_error).
    """
    warnings: list[str] = []
    if not text or not text.strip():
        return "", None, warnings, {
            "code": "DETAILED_OUTLINE_PARSE_ERROR",
            "message": "LLM output is empty",
        }

    value, _raw_json = extract_json_value(text)

    if isinstance(value, dict):
        content_md = str(value.get("content_md") or value.get("outline_md") or "").strip()
        if not content_md:
            content_md = text

        chapters_raw = value.get("chapters")
        if isinstance(chapters_raw, list) and chapters_raw:
            structure = {"chapters": _normalize_chapters(chapters_raw)}
            return content_md, structure, warnings, None

        # JSON found but no chapters key
        warnings.append("detailed_outline_no_chapters_in_json")
        return content_md, None, warnings, {
            "code": "DETAILED_OUTLINE_PARSE_ERROR",
            "message": "JSON found but missing 'chapters' array",
        }

    # No JSON found -- use raw text
    if likely_truncated_json(text):
        warnings.append("output_possibly_truncated")

    return text, None, warnings, {
        "code": "DETAILED_OUTLINE_PARSE_ERROR",
        "message": "Failed to extract JSON structure from LLM output",
    }


def _normalize_chapters(chapters_raw: list) -> list[dict[str, Any]]:
    """Normalize raw chapter dicts from LLM JSON output."""
    result: list[dict[str, Any]] = []
    for item in chapters_raw:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("number", 0))
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue

        title = str(item.get("title") or "")
        summary = str(item.get("summary") or "")
        beats_raw = item.get("beats") or []
        beats: list[str] = []
        if isinstance(beats_raw, list):
            beats = [str(b) for b in beats_raw if b is not None]

        entry: dict[str, Any] = {
            "number": number,
            "title": title,
            "summary": summary,
            "beats": beats,
        }
        # Preserve any extra keys the LLM may have added
        for k in ("scenes", "conflict", "resolution", "pov", "word_count_target"):
            if k in item:
                entry[k] = item[k]
        result.append(entry)

    return sorted(result, key=lambda c: c["number"])


def _upsert_detailed_outline(
    db: Session,
    *,
    outline_id: str,
    project_id: str,
    volume_number: int,
    volume_title: str,
    content_md: str,
    structure: dict[str, Any] | None,
) -> str:
    """Create or update a DetailedOutline row. Returns the row id."""
    existing = db.execute(
        select(DetailedOutline).where(
            DetailedOutline.outline_id == outline_id,
            DetailedOutline.volume_number == volume_number,
        )
    ).scalar_one_or_none()

    structure_json = json.dumps(structure, ensure_ascii=False) if structure is not None else None
    now = utc_now()

    if existing is not None:
        existing.volume_title = volume_title
        existing.content_md = content_md
        existing.structure_json = structure_json
        existing.status = "done"
        existing.updated_at = now
        db.commit()
        return existing.id

    row = DetailedOutline(
        id=new_id(),
        outline_id=outline_id,
        project_id=project_id,
        volume_number=volume_number,
        volume_title=volume_title,
        content_md=content_md,
        structure_json=structure_json,
        status="done",
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    return row.id


def _get_volume_summary(results: list[DetailedOutlineResult], idx: int) -> str | None:
    """Get a brief summary of a previously generated volume for context."""
    if idx < 0 or idx >= len(results):
        return None
    r = results[idx]
    parts: list[str] = []
    if r.volume_title:
        parts.append(f"Volume {r.volume_number}: {r.volume_title}")
    if r.structure is not None:
        chapters = r.structure.get("chapters")
        if isinstance(chapters, list):
            for ch in chapters[:5]:  # first 5 chapters as preview
                if isinstance(ch, dict):
                    ch_title = ch.get("title", "")
                    ch_summary = ch.get("summary", "")
                    parts.append(f"  Ch{ch.get('number', '?')}: {ch_title} - {ch_summary}")
            if len(chapters) > 5:
                parts.append(f"  ... ({len(chapters)} chapters total)")
    if not parts:
        # Fallback to content_md snippet
        snippet = (r.content_md or "")[:500]
        if snippet:
            parts.append(snippet)
    return "\n".join(parts) if parts else None


def _get_volume_beats_preview(volumes: list[VolumeInfo], idx: int) -> str | None:
    """Get the beats text of the next volume as a brief preview."""
    if idx < 0 or idx >= len(volumes):
        return None
    vol = volumes[idx]
    preview = (vol.beats_text or "")[:500]
    if vol.title:
        return f"Volume {vol.number}: {vol.title}\n{preview}"
    return preview if preview else None
