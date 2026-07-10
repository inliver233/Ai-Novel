from __future__ import annotations

import json

from app.services.outline_generation.policy import (
    OUTLINE_SEGMENT_INDEX_MAX_CHARS,
    OUTLINE_SEGMENT_INDEX_MAX_ITEMS,
    OUTLINE_SEGMENT_RECENT_CONTEXT_WINDOW,
    OUTLINE_SEGMENT_RECENT_WINDOW_MAX_CHARS,
    _build_outline_generation_guidance,
)

def _chapter_beats_count(chapter: dict[str, object]) -> int:
    beats_raw = chapter.get("beats")
    if not isinstance(beats_raw, list):
        return 0
    count = 0
    for beat in beats_raw:
        if isinstance(beat, str) and beat.strip():
            count += 1
    return count


def _outline_fill_detail_rule(*, target_chapter_count: int, existing_chapters: list[dict[str, object]]) -> str:
    base_rule = _build_outline_generation_guidance(target_chapter_count).get("chapter_detail_rule") or (
        "beats 每章 1~2 条，保持关键推进。"
    )
    beat_counts = sorted(count for count in (_chapter_beats_count(chapter) for chapter in existing_chapters) if count > 0)
    if not beat_counts:
        return base_rule
    median = beat_counts[len(beat_counts) // 2]
    low = max(1, median - 1)
    high = max(low, median + 1)
    if target_chapter_count > 120:
        low, high = min(low, 2), min(high, 2)
    elif target_chapter_count > 80:
        low, high = min(low, 2), min(high, 3)
    elif target_chapter_count > 40:
        low, high = min(low, 2), min(high, 4)
    else:
        low, high = min(low, 4), min(high, 6)
    consistency = (
        f"补全章节的 beats 粒度需尽量贴近已有章节（当前已生成章节 beats 中位数约 {median} 条）；"
        f"本轮建议每章 {low}~{high} 条。"
    )
    return f"{base_rule} {consistency}"

def _outline_fill_style_samples(existing_chapters: list[dict[str, object]]) -> str:
    if not existing_chapters:
        return "[]"
    total = len(existing_chapters)
    sample_indexes = sorted({0, min(1, total - 1), total // 2, total - 1})
    samples: list[dict[str, object]] = []
    for idx in sample_indexes:
        if idx < 0 or idx >= total:
            continue
        chapter = existing_chapters[idx]
        number = int(chapter.get("number") or 0)
        if number <= 0:
            continue
        beats: list[str] = []
        beats_raw = chapter.get("beats")
        if isinstance(beats_raw, list):
            for beat in beats_raw:
                text = str(beat).strip()
                if text:
                    beats.append(text[:42])
                if len(beats) >= 3:
                    break
        samples.append({"number": number, "title": str(chapter.get("title") or "")[:24], "beats": beats})
        if len(samples) >= 4:
            break
    return json.dumps(samples, ensure_ascii=False)

def _shrink_outline_segment_items(items: list[dict[str, object]], *, max_items: int, max_chars: int) -> list[dict[str, object]]:
    if not items:
        return []
    sampled = list(items)
    if len(sampled) > max_items:
        head = max(20, max_items // 2)
        tail = max_items - head
        sampled = [*sampled[:head], *sampled[-tail:]]
    payload = json.dumps({"items": sampled}, ensure_ascii=False)
    if len(payload) <= max_chars:
        return sampled
    compact = list(sampled)
    while len(compact) > 32:
        compact = [*compact[: len(compact) // 2], *compact[-max(1, len(compact) // 4) :]]
        payload = json.dumps({"items": compact}, ensure_ascii=False)
        if len(payload) <= max_chars:
            return compact
    return compact

def _build_outline_segment_chapter_index(chapters: list[dict[str, object]]) -> str:
    items = []
    for chapter in chapters:
        try:
            number = int(chapter.get("number"))
        except Exception:
            continue
        if number <= 0:
            continue
        items.append({"number": number, "title": str(chapter.get("title") or "").strip()[:28]})
    items.sort(key=lambda row: int(row.get("number") or 0))
    total = len(items)
    sampled = _shrink_outline_segment_items(
        items,
        max_items=OUTLINE_SEGMENT_INDEX_MAX_ITEMS,
        max_chars=OUTLINE_SEGMENT_INDEX_MAX_CHARS,
    )
    payload: dict[str, object] = {"total": total, "items": sampled}
    omitted = total - len(sampled)
    if omitted > 0:
        payload["omitted"] = omitted
    return json.dumps(payload, ensure_ascii=False)

def _build_outline_segment_recent_window(chapters: list[dict[str, object]]) -> str:
    if not chapters:
        return "[]"
    window = chapters[-OUTLINE_SEGMENT_RECENT_CONTEXT_WINDOW:]
    items: list[dict[str, object]] = []
    for chapter in window:
        try:
            number = int(chapter.get("number"))
        except Exception:
            continue
        if number <= 0:
            continue
        beats: list[str] = []
        beats_raw = chapter.get("beats")
        if isinstance(beats_raw, list):
            for beat in beats_raw:
                text = str(beat).strip()
                if text:
                    beats.append(text[:64])
                if len(beats) >= 3:
                    break
        items.append({"number": number, "title": str(chapter.get("title") or "").strip()[:28], "beats": beats})
    text = json.dumps(items, ensure_ascii=False)
    if len(text) <= OUTLINE_SEGMENT_RECENT_WINDOW_MAX_CHARS:
        return text
    compact = [
        {
            "number": int(row.get("number") or 0),
            "title": str(row.get("title") or "")[:18],
            "beats": [str(x)[:40] for x in (row.get("beats") if isinstance(row.get("beats"), list) else [])[:2]],
        }
        for row in items
    ]
    return json.dumps(compact, ensure_ascii=False)

def _extract_outline_chapter_numbers(chapters: list[dict[str, object]], *, limit: int = 64) -> list[int]:
    numbers: set[int] = set()
    for chapter in chapters:
        try:
            number = int(chapter.get("number"))
        except Exception:
            continue
        if number <= 0:
            continue
        numbers.add(number)
        if len(numbers) >= limit:
            break
    return sorted(numbers)

def _chapter_score(chapter: dict[str, object]) -> int:
    title = str(chapter.get("title") or "").strip()
    beats = chapter.get("beats")
    beats_count = len(beats) if isinstance(beats, list) else 0
    return len(title) + beats_count

def _merge_segment_chapters(
    *,
    by_number: dict[int, dict[str, object]],
    incoming: list[dict[str, object]],
    allowed_numbers: set[int],
) -> tuple[int, list[int]]:
    accepted = 0
    accepted_numbers: list[int] = []
    for chapter in incoming:
        number = int(chapter.get("number") or 0)
        if number <= 0 or number not in allowed_numbers:
            continue
        previous = by_number.get(number)
        if previous is None:
            by_number[number] = chapter
            accepted += 1
            accepted_numbers.append(number)
            continue
        if _chapter_score(chapter) > _chapter_score(previous):
            by_number[number] = chapter
    return accepted, accepted_numbers

def _dedupe_warnings(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, str) and item not in seen:
            seen.add(item)
            out.append(item)
    return out

def _normalize_outline_chapters(chapters: object) -> tuple[list[dict[str, object]], list[str]]:
    if not isinstance(chapters, list):
        return [], []
    warnings: list[str] = []
    by_number: dict[int, dict[str, object]] = {}
    dropped_invalid = 0
    dropped_non_positive = 0
    deduped = 0
    for item in chapters:
        if not isinstance(item, dict):
            dropped_invalid += 1
            continue
        try:
            number = int(item.get("number"))
        except Exception:
            dropped_invalid += 1
            continue
        if number <= 0:
            dropped_non_positive += 1
            continue
        beats: list[str] = []
        beats_raw = item.get("beats")
        if isinstance(beats_raw, list):
            for beat in beats_raw:
                if beat is None:
                    continue
                text = str(beat).strip()
                if text:
                    beats.append(text)
        elif isinstance(beats_raw, str):
            text = beats_raw.strip()
            if text:
                beats.append(text)
        chapter = {"number": number, "title": str(item.get("title") or "").strip(), "beats": beats}
        existing = by_number.get(number)
        if existing is None:
            by_number[number] = chapter
            continue
        deduped += 1
        existing_score = len(str(existing.get("title") or "").strip()) + len(existing.get("beats") or [])
        next_score = len(chapter["title"]) + len(beats)
        if next_score > existing_score:
            by_number[number] = chapter
    if dropped_invalid:
        warnings.append("outline_chapter_invalid_filtered")
    if dropped_non_positive:
        warnings.append("outline_chapter_non_positive_filtered")
    if deduped:
        warnings.append("outline_chapter_number_deduped")
    return [by_number[n] for n in sorted(by_number.keys())], warnings

def _clone_outline_chapters(chapters: list[dict[str, object]]) -> list[dict[str, object]]:
    cloned = []
    for chapter in chapters:
        try:
            number = int(chapter.get("number"))
        except Exception:
            continue
        beats: list[str] = []
        beats_raw = chapter.get("beats")
        if isinstance(beats_raw, list):
            for beat in beats_raw:
                text = str(beat).strip()
                if text:
                    beats.append(text)
        cloned.append({"number": number, "title": str(chapter.get("title") or ""), "beats": beats})
    return cloned

def _collect_missing_chapter_numbers(chapters: list[dict[str, object]], target_chapter_count: int) -> list[int]:
    existing_numbers: set[int] = set()
    for chapter in chapters:
        try:
            number = int(chapter.get("number"))
        except Exception:
            continue
        if 1 <= number <= target_chapter_count:
            existing_numbers.add(number)
    return [n for n in range(1, target_chapter_count + 1) if n not in existing_numbers]

def _format_chapter_number_ranges(numbers: list[int]) -> str:
    if not numbers:
        return ""
    nums = sorted(set(int(n) for n in numbers))
    ranges: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        ranges.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = n
    ranges.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(ranges)

def _compact_neighbor_chapter(chapter: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(chapter, dict):
        return None
    try:
        number = int(chapter.get("number"))
    except Exception:
        return None
    if number <= 0:
        return None
    beats: list[str] = []
    beats_raw = chapter.get("beats")
    if isinstance(beats_raw, list):
        for beat in beats_raw:
            text = str(beat).strip()
            if text:
                beats.append(text[:52])
            if len(beats) >= 2:
                break
    return {"number": number, "title": str(chapter.get("title") or "")[:28], "beats": beats}

def _build_missing_neighbor_context(
    existing_chapters: list[dict[str, object]],
    missing_numbers: list[int],
    *,
    max_items: int = 24,
    max_chars: int = 2400,
) -> str:
    if not existing_chapters or not missing_numbers:
        return "[]"
    by_number: dict[int, dict[str, object]] = {}
    for chapter in existing_chapters:
        try:
            number = int(chapter.get("number"))
        except Exception:
            continue
        if number > 0:
            by_number[number] = chapter
    contexts: list[dict[str, object]] = []
    for number in sorted(set(int(n) for n in missing_numbers if int(n) > 0)):
        row: dict[str, object] = {"number": number}
        prev_compact = _compact_neighbor_chapter(by_number.get(number - 1))
        next_compact = _compact_neighbor_chapter(by_number.get(number + 1))
        if prev_compact is not None:
            row["prev"] = prev_compact
        if next_compact is not None:
            row["next"] = next_compact
        contexts.append(row)
        if len(contexts) >= max_items:
            break
    text = json.dumps(contexts, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    compact_rows: list[dict[str, object]] = []
    for row in contexts:
        slim: dict[str, object] = {"number": int(row.get("number") or 0)}
        prev_row = row.get("prev")
        if isinstance(prev_row, dict):
            slim["prev"] = {"number": int(prev_row.get("number") or 0), "title": str(prev_row.get("title") or "")[:18]}
        next_row = row.get("next")
        if isinstance(next_row, dict):
            slim["next"] = {"number": int(next_row.get("number") or 0), "title": str(next_row.get("title") or "")[:18]}
        compact_rows.append(slim)
    return json.dumps(compact_rows, ensure_ascii=False)


def _enforce_outline_chapter_coverage(
    *,
    data: dict[str, object],
    target_chapter_count: int | None,
) -> tuple[dict[str, object], list[str]]:
    if not target_chapter_count or target_chapter_count <= 0:
        return data, []
    normalized, warnings = _normalize_outline_chapters(data.get("chapters"))
    if not normalized:
        return data, warnings
    by_number: dict[int, dict[str, object]] = {}
    filtered_beyond_target = 0
    for chapter in normalized:
        number = int(chapter["number"])
        if number > target_chapter_count:
            filtered_beyond_target += 1
            continue
        by_number[number] = chapter
    if filtered_beyond_target:
        warnings.append("outline_chapter_beyond_target_filtered")
    chapters_out = [by_number[n] for n in sorted(by_number.keys())]
    missing_numbers = _collect_missing_chapter_numbers(chapters_out, target_chapter_count=target_chapter_count)
    data["chapter_coverage"] = {
        "target_chapter_count": target_chapter_count,
        "parsed_chapter_count": len(chapters_out),
        "missing_count": len(missing_numbers),
        "missing_numbers": missing_numbers,
    }
    if missing_numbers:
        warnings.append("outline_chapter_coverage_incomplete")
    data["chapters"] = chapters_out
    return data, warnings
