from __future__ import annotations

import json
import re

from app.services.outline_generation.chapter_ops import (
    _build_missing_neighbor_context,
    _build_outline_segment_chapter_index,
    _build_outline_segment_recent_window,
    _format_chapter_number_ranges,
    _normalize_outline_chapters,
    _outline_fill_detail_rule,
    _outline_fill_style_samples,
)
from app.services.outline_generation.policy import OUTLINE_STREAM_RAW_PREVIEW_MAX_CHARS
from app.services.output_parsers import extract_json_value, likely_truncated_json


def _strip_segment_conflicting_prompt_sections(text: str) -> str:
    if not text.strip():
        return text
    return re.sub(r"(?is)<\s*CHAPTER_TARGET\s*>[\s\S]*?<\s*/\s*CHAPTER_TARGET\s*>", "", text).strip()


def _build_outline_stream_raw_preview(text: object, *, max_chars: int = OUTLINE_STREAM_RAW_PREVIEW_MAX_CHARS) -> str:
    if not isinstance(text, str):
        return ""
    cleaned = text.strip()
    if not cleaned:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned
    omitted = len(cleaned) - max_chars
    return f"{cleaned[:max_chars]}\n...(已截断 {omitted} 字符)"


def _parse_outline_batch_output(
    *,
    text: str,
    finish_reason: str | None = None,
    fallback_outline_md: str | None = None,
) -> tuple[dict[str, object], list[str], dict[str, object] | None]:
    warnings: list[str] = []
    value, raw_json = extract_json_value(text)
    if not isinstance(value, dict):
        parse_error: dict[str, object] = {"code": "OUTLINE_PARSE_ERROR", "message": "无法从模型输出解析章节结构"}
        if finish_reason == "length" or likely_truncated_json(text):
            parse_error["hint"] = "输出疑似被截断（JSON 未闭合），将自动重试当前分段"
        return {"outline_md": str(fallback_outline_md or ""), "chapters": [], "raw_output": text}, warnings, parse_error

    outline_md_raw = value.get("outline_md")
    outline_md = outline_md_raw.strip() if isinstance(outline_md_raw, str) else ""
    if not outline_md and isinstance(fallback_outline_md, str):
        outline_md = fallback_outline_md.strip()
    chapters_out, chapter_warnings = _normalize_outline_chapters(value.get("chapters"))
    warnings.extend(chapter_warnings)
    if finish_reason == "length":
        warnings.append("output_truncated")
    data: dict[str, object] = {"outline_md": outline_md, "chapters": chapters_out, "raw_output": text}
    if raw_json:
        data["raw_json"] = raw_json
    if chapters_out:
        return data, warnings, None
    parse_error = {"code": "OUTLINE_PARSE_ERROR", "message": "无法从模型输出解析章节结构"}
    if finish_reason == "length" or likely_truncated_json(text):
        parse_error["hint"] = "输出疑似被截断（JSON 未闭合），将自动重试当前分段"
    return data, warnings, parse_error


def _build_outline_segment_prompts(
    *,
    base_prompt_system: str,
    base_prompt_user: str,
    target_chapter_count: int,
    batch_numbers: list[int],
    existing_chapters: list[dict[str, object]],
    existing_outline_md: str,
    attempt: int,
    max_attempts: int,
    previous_output_numbers: list[int] | None = None,
    previous_failure_reason: str | None = None,
) -> tuple[str, str]:
    base_user = _strip_segment_conflicting_prompt_sections(base_prompt_user)
    missing_ranges = _format_chapter_number_ranges(batch_numbers)
    missing_numbers_json = json.dumps(batch_numbers, ensure_ascii=False)
    detail_rule = _outline_fill_detail_rule(target_chapter_count=target_chapter_count, existing_chapters=existing_chapters)

    existing_numbers = sorted(
        {
            int(chapter.get("number"))
            for chapter in existing_chapters
            if str(chapter.get("number") or "").strip().isdigit() and int(chapter.get("number")) > 0
        }
    )
    existing_ranges = _format_chapter_number_ranges(existing_numbers)
    chapter_index = _build_outline_segment_chapter_index(existing_chapters)
    recent_window = _build_outline_segment_recent_window(existing_chapters)
    outline_anchor = (existing_outline_md or "").strip()[:3600]

    feedback_block = ""
    if attempt > 1:
        prev_numbers_text = _format_chapter_number_ranges(previous_output_numbers or []) or "（无可识别章号）"
        failure_reason = (previous_failure_reason or "上一轮输出未满足当前批次约束").strip()
        feedback_block = (
            "<LAST_ATTEMPT_FEEDBACK>\n"
            f"上一轮失败原因：{failure_reason}\n"
            f"上一轮输出章号：{prev_numbers_text}\n"
            "本轮必须纠正：只输出当前批次章号数组对应的章节。\n"
            "</LAST_ATTEMPT_FEEDBACK>\n"
        )

    system = (
        f"{base_prompt_system}\n\n"
        "[分段生成协议]\n"
        "你现在处于“长篇章节分段生成”模式。\n"
        "你必须只输出一个 JSON 对象，禁止任何解释、Markdown、代码块。\n"
        'JSON 固定为：{"outline_md": string, "chapters":[{"number":int,"title":string,"beats":[string]}]}。\n'
        "本轮只能输出要求章号，不能输出范围外章节。\n"
        "本轮要求的每个章号必须出现且仅出现一次。\n"
        "不得输出占位内容（如 TODO/待补全/略）。\n"
    )
    user = (
        f"{base_user}\n\n"
        "<SEGMENT_TASK>\n"
        f"目标总章数：{target_chapter_count}\n"
        f"当前批次缺失章号：{missing_ranges}\n"
        f"当前批次章号数组（严格按此输出）：{missing_numbers_json}\n"
        f"已完成章号（禁止输出）：{existing_ranges or '（空）'}\n"
        f"当前尝试：第 {attempt}/{max_attempts} 轮（仅补当前批次缺失章号）\n"
        f"已生成章节标题索引（全量，不可改写）：{chapter_index}\n"
        f"最近章节细节（用于衔接语义）：{recent_window}\n"
        f"全书总纲锚点（不可改写）：{outline_anchor}\n"
        f"每章细节规则：{detail_rule}\n"
        f"{feedback_block}"
        "输出要求：\n"
        "- chapters 只能包含当前批次缺失章号，且必须全部覆盖。\n"
        "- number 必须严格等于指定章号，不得跳号/重号。\n"
        "- 若输出任何已完成章号或范围外章号，本轮会被判定失败并重试。\n"
        "- title 简洁明确，beats 使用短句、强调因果推进。\n"
        "- outline_md 可沿用既有总纲，不得输出空对象或额外字段。\n"
        "- 输出前自检：chapters.number 集合必须与当前批次章号数组完全一致。\n"
        "</SEGMENT_TASK>"
    )
    return system, user


def _build_outline_missing_chapters_prompts(
    *,
    target_chapter_count: int,
    missing_numbers: list[int],
    existing_chapters: list[dict[str, object]],
    outline_md: str,
) -> tuple[str, str]:
    fill_detail_rule = _outline_fill_detail_rule(target_chapter_count=target_chapter_count, existing_chapters=existing_chapters)
    missing_numbers_json = json.dumps(sorted(set(int(n) for n in missing_numbers if int(n) > 0)), ensure_ascii=False)
    existing_numbers = sorted(
        {
            int(chapter.get("number"))
            for chapter in existing_chapters
            if str(chapter.get("number") or "").strip().isdigit() and int(chapter.get("number")) > 0
        }
    )
    existing_ranges = _format_chapter_number_ranges(existing_numbers)
    neighbor_context = _build_missing_neighbor_context(existing_chapters, missing_numbers)
    style_samples = _outline_fill_style_samples(existing_chapters)

    system = (
        "你是严谨的长篇大纲补全器。"
        "你必须只输出一个 JSON 对象，禁止任何解释、Markdown、代码块。"
        '输出格式固定为：{"chapters":[{"number":int,"title":string,"beats":[string]}]}。'
        "仅输出请求的缺失章号，每个章号出现且仅出现一次。"
        "禁止输出‘待补全/自动补齐/占位/TODO’等占位词。"
        "每个 beats 必须是具体事件，避免空泛总结。"
    )
    compact = [{"number": int(c["number"]), "title": str(c.get("title") or "")[:24]} for c in existing_chapters if "number" in c]
    if len(compact) > 60:
        compact = [*compact[:30], *compact[-30:]]
    user = (
        f"目标总章数：{target_chapter_count}\n"
        f"缺失章号：{_format_chapter_number_ranges(missing_numbers)}\n"
        f"缺失章号数组（严格按此输出）：{missing_numbers_json}\n"
        f"已完成章号（禁止输出）：{existing_ranges or '（空）'}\n"
        f"已有章节（仅供连续性参考，不可重写）：{json.dumps(compact, ensure_ascii=False)}\n"
        f"缺失章节邻接上下文（prev/next，仅供衔接）：{neighbor_context}\n"
        f"风格参考样本（模仿细节密度与句式，不得复用剧情）：{style_samples}\n"
        f"整体梗概（节选）：{(outline_md or '')[:2500]}\n\n"
        "请只输出缺失章号对应的 chapters。\n"
        "输出前自检：chapters.number 集合必须与缺失章号数组完全一致。\n"
        f"每章要求：title 简洁；{fill_detail_rule}"
    )
    return system, user


def _build_outline_gap_repair_prompts(
    *,
    target_chapter_count: int,
    batch_missing: list[int],
    existing_chapters: list[dict[str, object]],
    outline_md: str,
    attempt: int,
    max_attempts: int,
    previous_output_numbers: list[int] | None = None,
    previous_failure_reason: str | None = None,
) -> tuple[str, str]:
    missing_sorted = sorted(set(int(n) for n in batch_missing if int(n) > 0))
    missing_json = json.dumps(missing_sorted, ensure_ascii=False)
    missing_ranges = _format_chapter_number_ranges(missing_sorted)
    index_json = _build_outline_segment_chapter_index(existing_chapters)
    neighbor_context = _build_missing_neighbor_context(existing_chapters, missing_sorted, max_items=12, max_chars=1800)
    style_samples = _outline_fill_style_samples(existing_chapters)
    detail_rule = _outline_fill_detail_rule(target_chapter_count=target_chapter_count, existing_chapters=existing_chapters)

    feedback_block = ""
    if attempt > 1:
        prev_numbers_text = _format_chapter_number_ranges(previous_output_numbers or []) or "（无可识别章号）"
        reason = (previous_failure_reason or "上一轮未产生可采纳章节").strip()
        feedback_block = (
            "<LAST_ATTEMPT_FEEDBACK>\n"
            f"上一轮失败原因：{reason}\n"
            f"上一轮输出章号：{prev_numbers_text}\n"
            "本轮必须只输出当前批次缺失章号数组。\n"
            "</LAST_ATTEMPT_FEEDBACK>\n"
        )

    system = (
        "你是长篇大纲终检补全器。"
        "你必须只输出一个 JSON 对象，禁止任何解释、Markdown、代码块。"
        '输出格式固定为：{"chapters":[{"number":int,"title":string,"beats":[string]}]}。'
        "本轮只能输出要求章号，每个章号出现且仅出现一次。"
        "禁止输出范围外章号、禁止输出空 beats、禁止占位词。"
    )
    user = (
        f"目标总章数：{target_chapter_count}\n"
        f"本轮缺失章号：{missing_ranges}\n"
        f"本轮缺失章号数组（严格按此输出）：{missing_json}\n"
        f"全量章节索引（不可改写）：{index_json}\n"
        f"缺失章节邻接上下文（prev/next）：{neighbor_context}\n"
        f"风格参考样本：{style_samples}\n"
        f"整体梗概（节选）：{(outline_md or '')[:2400]}\n"
        f"当前尝试：第 {attempt}/{max_attempts} 轮\n"
        f"{feedback_block}"
        "输出前自检：chapters.number 集合必须与本轮缺失章号数组完全一致。\n"
        f"每章要求：title 简洁；{detail_rule}"
    )
    return system, user
