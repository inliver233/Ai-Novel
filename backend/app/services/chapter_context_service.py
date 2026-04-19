from __future__ import annotations

import json
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.character import Character
from app.models.detailed_outline import DetailedOutline
from app.models.entry import Entry
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_settings import ProjectSettings
from app.schemas.chapter_generate import ChapterGenerateContext, ChapterGenerateRequest
from app.services.prompt_store import format_characters
from app.services.style_resolution_service import resolve_style_guide

PREVIOUS_CHAPTER_ENDING_CHARS = 1000
CURRENT_DRAFT_TAIL_CHARS = 1200

SMART_CONTEXT_RECENT_SUMMARIES_MAX = 20
SMART_CONTEXT_RECENT_FULL_MAX = 2
SMART_CONTEXT_RECENT_FULL_HEAD_CHARS = 1200
SMART_CONTEXT_RECENT_FULL_TAIL_CHARS = 1200
SMART_CONTEXT_SKELETON_STRIDE_SMALL = 10
SMART_CONTEXT_SKELETON_STRIDE_LARGE = 20
SMART_CONTEXT_SKELETON_LARGE_THRESHOLD = 80


def build_smart_context(
    db: Session,
    *,
    project_id: str,
    outline_id: str,
    chapter_number: int,
) -> tuple[str, str, str]:
    if chapter_number <= 1:
        return "", "", ""

    summary_rows = db.execute(
        select(Chapter.number, Chapter.title, Chapter.summary)
        .where(
            Chapter.project_id == project_id,
            Chapter.outline_id == outline_id,
            Chapter.number < chapter_number,
        )
        .order_by(Chapter.number.desc())
        .limit(SMART_CONTEXT_RECENT_SUMMARIES_MAX)
    ).all()
    summary_rows.reverse()
    recent_summary_lines: list[str] = []
    for num, title, summary in summary_rows:
        text = (summary or "").strip()
        if not text:
            continue
        title_str = (title or "").strip()
        head = f"第{num}章 {title_str}" if title_str else f"第{num}章"
        recent_summary_lines.append(f"- {head}：{text}")
    recent_summaries = "\n".join(recent_summary_lines).strip()

    full_rows = db.execute(
        select(Chapter.number, Chapter.title, Chapter.content_md)
        .where(
            Chapter.project_id == project_id,
            Chapter.outline_id == outline_id,
            Chapter.number < chapter_number,
        )
        .order_by(Chapter.number.desc())
        .limit(SMART_CONTEXT_RECENT_FULL_MAX)
    ).all()
    full_rows.reverse()
    recent_full_parts: list[str] = []
    for num, title, content_md in full_rows:
        raw = (content_md or "").strip()
        if not raw:
            continue
        title_str = (title or "").strip()
        head = f"第{num}章 {title_str}" if title_str else f"第{num}章"
        if len(raw) <= SMART_CONTEXT_RECENT_FULL_HEAD_CHARS + SMART_CONTEXT_RECENT_FULL_TAIL_CHARS + 80:
            snippet = raw
        else:
            snippet = (
                raw[:SMART_CONTEXT_RECENT_FULL_HEAD_CHARS].rstrip()
                + "\n...\n"
                + raw[-SMART_CONTEXT_RECENT_FULL_TAIL_CHARS :].lstrip()
            )
        recent_full_parts.append(f"【{head} 正文节选】\n{snippet}")
    recent_full = "\n\n".join(recent_full_parts).strip()

    total_prev = max(0, chapter_number - 1)
    stride = (
        SMART_CONTEXT_SKELETON_STRIDE_LARGE
        if total_prev >= SMART_CONTEXT_SKELETON_LARGE_THRESHOLD
        else SMART_CONTEXT_SKELETON_STRIDE_SMALL
    )
    skeleton_numbers = [n for n in range(1, chapter_number, stride)]

    skeleton = ""
    if len(skeleton_numbers) >= 2:
        skeleton_rows = db.execute(
            select(Chapter.number, Chapter.title, Chapter.summary, Chapter.plan)
            .where(
                Chapter.project_id == project_id,
                Chapter.outline_id == outline_id,
                Chapter.number.in_(skeleton_numbers),
            )
            .order_by(Chapter.number.asc())
        ).all()
        skeleton_lines: list[str] = []
        for num, title, summary, plan in skeleton_rows:
            text = (summary or "").strip() or (plan or "").strip()
            if not text:
                continue
            title_str = (title or "").strip()
            head = f"第{num}章 {title_str}" if title_str else f"第{num}章"
            skeleton_lines.append(f"- {head}：{text}")
        skeleton = "\n".join(skeleton_lines).strip()

    return recent_summaries, recent_full, skeleton


def load_detailed_outline_context(
    chapter_number: int,
    outline_id: str,
    db: Session,
) -> str:
    """Load detailed outline context for a chapter.

    Queries DetailedOutline records for the given outline_id (status='done'),
    finds which volume contains the target chapter_number, then extracts:
      - Current chapter plan (summary + beats)
      - Previous 2 and next 2 chapter summaries from the same volume
      - Volume-level title
    Returns a formatted text block, or empty string if nothing found.
    """
    rows = (
        db.execute(
            select(DetailedOutline).where(
                DetailedOutline.outline_id == outline_id,
                DetailedOutline.status == "done",
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return ""

    # Find which volume contains this chapter_number
    target_volume: DetailedOutline | None = None
    target_chapters: list[dict] = []
    for row in rows:
        chapters = _parse_detailed_structure_chapters(row.structure_json)
        if not chapters:
            continue
        chapter_numbers = {ch.get("number") for ch in chapters if isinstance(ch.get("number"), int)}
        if chapter_number in chapter_numbers:
            target_volume = row
            target_chapters = chapters
            break

    if target_volume is None or not target_chapters:
        return ""

    # Sort chapters by number for consistent ordering
    target_chapters.sort(key=lambda ch: ch.get("number", 0))

    # Build a lookup by chapter number
    by_number: dict[int, dict] = {ch["number"]: ch for ch in target_chapters if isinstance(ch.get("number"), int)}

    parts: list[str] = []

    # Volume header
    vol_num = target_volume.volume_number or 1
    vol_title = (target_volume.volume_title or "").strip()
    vol_header = f"\u7b2c{vol_num}\u5377"
    if vol_title:
        vol_header += f"\u300c{vol_title}\u300d"
    parts.append(f"\u3010\u6240\u5c5e\u5377\u3011{vol_header}")

    # Current chapter plan
    current_ch = by_number.get(chapter_number)
    if current_ch:
        parts.append(f"\n\u3010\u5f53\u524d\u7ae0\u8282\u89c4\u5212\u3011\u7b2c{chapter_number}\u7ae0")
        ch_summary = str(current_ch.get("summary") or "").strip()
        if ch_summary:
            parts.append(f"\u6982\u8ff0\uff1a{ch_summary}")
        beats = current_ch.get("beats")
        if isinstance(beats, list) and beats:
            parts.append("\u60c5\u8282\u70b9\uff1a")
            for b in beats:
                if b is not None:
                    parts.append(f"- {str(b)}")
        # Include extra keys if present
        characters = current_ch.get("characters")
        if characters:
            if isinstance(characters, list):
                joined_characters = "，".join(str(c) for c in characters)
                parts.append(f"\u51fa\u573a\u89d2\u8272\uff1a{joined_characters}")
            else:
                parts.append(f"\u51fa\u573a\u89d2\u8272\uff1a{characters}")
        emotional = current_ch.get("emotional_arc") or current_ch.get("emotion")
        if emotional:
            parts.append(f"\u60c5\u611f\u8d70\u5411\uff1a{emotional}")

    # Previous chapters context (up to 2)
    prev_lines: list[str] = []
    all_numbers = sorted(by_number.keys())
    current_idx = all_numbers.index(chapter_number) if chapter_number in all_numbers else -1
    if current_idx > 0:
        prev_nums = all_numbers[max(0, current_idx - 2):current_idx]
        for pn in prev_nums:
            pch = by_number[pn]
            pt = str(pch.get("title") or "").strip()
            ps = str(pch.get("summary") or "").strip()
            label = f"\u7b2c{pn}\u7ae0"
            if pt:
                label += f"\u300c{pt}\u300d"
            if ps:
                prev_lines.append(f"{label}\uff1a{ps}")
    if prev_lines:
        parts.append(f"\n\u3010\u524d\u6587\u8d70\u5411\u3011")
        parts.extend(prev_lines)

    # Next chapters context (up to 2)
    next_lines: list[str] = []
    if current_idx >= 0 and current_idx < len(all_numbers) - 1:
        next_nums = all_numbers[current_idx + 1:current_idx + 3]
        for nn in next_nums:
            nch = by_number[nn]
            nt = str(nch.get("title") or "").strip()
            ns = str(nch.get("summary") or "").strip()
            label = f"\u7b2c{nn}\u7ae0"
            if nt:
                label += f"\u300c{nt}\u300d"
            if ns:
                next_lines.append(f"{label}\uff1a{ns}")
    if next_lines:
        parts.append(f"\n\u3010\u540e\u6587\u8d70\u5411\u3011")
        parts.extend(next_lines)

    return "\n".join(parts).strip()


def _parse_detailed_structure_chapters(structure_json_raw: str | None) -> list[dict]:
    """Parse structure_json and return the chapters list, or empty list."""
    if not structure_json_raw:
        return []
    try:
        structure = json.loads(structure_json_raw)
    except Exception:
        return []
    if not isinstance(structure, dict):
        return []
    chapters = structure.get("chapters")
    if not isinstance(chapters, list):
        return []
    return [ch for ch in chapters if isinstance(ch, dict)]


def load_previous_chapter_context(
    db: Session,
    *,
    project_id: str,
    outline_id: str,
    chapter_number: int,
    previous_chapter: str | None,
) -> tuple[str, str]:
    mode = previous_chapter or "none"
    if mode == "none" or chapter_number <= 1:
        return "", ""

    prev = (
        db.execute(
            select(Chapter).where(
                Chapter.project_id == project_id,
                Chapter.outline_id == outline_id,
                Chapter.number == (chapter_number - 1),
            )
        )
        .scalars()
        .first()
    )
    if prev is None:
        return "", ""

    if mode == "summary":
        return (prev.summary or "").strip(), ""
    if mode == "content":
        return (prev.content_md or "").strip(), ""
    if mode == "tail":
        raw = (prev.content_md or "").strip()
        if not raw:
            return "", ""
        tail = raw[-PREVIOUS_CHAPTER_ENDING_CHARS:].lstrip()
        return "", tail

    return "", ""


def resolve_current_draft_tail(*, chapter: Chapter, request_tail: str | None) -> str:
    if request_tail is not None and request_tail.strip():
        return request_tail.strip()[-CURRENT_DRAFT_TAIL_CHARS:].lstrip()
    raw = (chapter.content_md or "").strip()
    if not raw:
        return ""
    return raw[-CURRENT_DRAFT_TAIL_CHARS:].lstrip()


def _parse_entry_tags(tags_json: str | None) -> list[str]:
    if not tags_json:
        return []
    try:
        value = json.loads(tags_json)
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


def _format_entries(entries: list[Entry]) -> str:
    if not entries:
        return ""
    parts: list[str] = []
    for entry in entries:
        title = str(getattr(entry, "title", "") or "").strip() or "无标题"
        tags = _parse_entry_tags(getattr(entry, "tags_json", None))
        tag_str = "、".join(tags) if tags else ""
        content = str(getattr(entry, "content", "") or "").strip()
        header = f"### {title}"
        if tag_str:
            header = f"### [{tag_str}] {title}"
        parts.append(f"{header}\n{content}".rstrip())
    return "\n\n".join(parts)


def _load_project_story_text_context(
    db: Session,
    *,
    project_id: str,
    outline_id: str,
    ctx: ChapterGenerateContext,
) -> tuple[str, str, str, str, str, str]:
    settings_row = db.get(ProjectSettings, project_id)
    outline_row = db.get(Outline, outline_id)

    world_setting = (settings_row.world_setting if settings_row else "") or ""
    style_guide = (settings_row.style_guide if settings_row else "") or ""
    constraints = (settings_row.constraints if settings_row else "") or ""

    if not ctx.include_world_setting:
        world_setting = ""
    if not ctx.include_style_guide:
        style_guide = ""
    if not ctx.include_constraints:
        constraints = ""

    outline_text = (outline_row.content_md if outline_row else "") or ""
    if not ctx.include_outline:
        outline_text = ""

    chars: list[Character] = []
    if ctx.character_ids:
        chars = (
            db.execute(
                select(Character).where(
                    Character.project_id == project_id,
                    Character.id.in_(ctx.character_ids),
                )
            )
            .scalars()
            .all()
        )
    characters_text = format_characters(chars)
    entries: list[Entry] = []
    if ctx.entry_ids:
        fetched_entries = (
            db.execute(
                select(Entry).where(
                    Entry.project_id == project_id,
                    Entry.id.in_(ctx.entry_ids),
                )
            )
            .scalars()
            .all()
        )
        entries_by_id = {str(entry.id): entry for entry in fetched_entries}
        seen_entry_ids: set[str] = set()
        entries = []
        for entry_id in ctx.entry_ids:
            if entry_id in seen_entry_ids:
                continue
            entry = entries_by_id.get(entry_id)
            if entry is None:
                continue
            seen_entry_ids.add(entry_id)
            entries.append(entry)
    entries_text = _format_entries(entries)

    return world_setting, style_guide, constraints, outline_text, characters_text, entries_text


def _format_chapter_generate_instruction(*, mode: Literal["replace", "append"], base_instruction: str) -> str:
    instruction = base_instruction
    if mode == "append":
        instruction = "【追加模式】只输出需要追加到正文末尾的新增片段，不要重复已写内容。\\n" + instruction
    else:
        instruction = "【替换模式】输出完整替换稿（整章）。\\n" + instruction
    return instruction


def assemble_chapter_generate_render_values(
    *,
    project: Project,
    mode: Literal["replace", "append"],
    chapter_number: int,
    chapter_title: str,
    chapter_plan: str,
    world_setting: str,
    style_guide: str,
    constraints: str,
    characters_text: str,
    entries_text: str,
    outline_text: str,
    instruction: str,
    target_word_count: int | None,
    previous_chapter: str,
    previous_chapter_ending: str,
    current_draft_tail: str,
    smart_context_recent_summaries: str,
    smart_context_recent_full: str,
    smart_context_story_skeleton: str,
    detailed_outline_context: str = "",
) -> tuple[dict[str, object], dict[str, object]]:
    requirements_obj: dict[str, object] = {}
    if target_word_count is not None:
        requirements_obj["target_word_count"] = target_word_count
    requirements_text = json.dumps(requirements_obj, ensure_ascii=False, indent=2) if requirements_obj else ""

    values: dict[str, object] = {
        "mode": mode,
        "project_name": project.name or "",
        "genre": project.genre or "",
        "logline": project.logline or "",
        "world_setting": world_setting,
        "style_guide": style_guide,
        "constraints": constraints,
        "characters": characters_text,
        "entries": entries_text,
        "outline": outline_text,
        "chapter_number": str(chapter_number),
        "chapter_title": chapter_title,
        "chapter_plan": chapter_plan,
        "requirements": requirements_text,
        "target_word_count": str(target_word_count or ""),
        "instruction": instruction,
        "previous_chapter": previous_chapter,
        "previous_chapter_ending": previous_chapter_ending,
        "current_draft_tail": current_draft_tail,
        "smart_context_recent_summaries": smart_context_recent_summaries,
        "smart_context_recent_full": smart_context_recent_full,
        "smart_context_story_skeleton": smart_context_story_skeleton,
        "detailed_outline_context": detailed_outline_context,
    }
    values["project"] = {
        "name": project.name or "",
        "genre": project.genre or "",
        "logline": project.logline or "",
        "world_setting": world_setting,
        "style_guide": style_guide,
        "constraints": constraints,
        "characters": characters_text,
        "entries": entries_text,
    }
    values["story"] = {
        "outline": outline_text,
        "chapter_number": int(chapter_number),
        "chapter_title": chapter_title,
        "chapter_plan": chapter_plan,
        "previous_chapter": previous_chapter,
        "previous_chapter_ending": previous_chapter_ending,
        "mode": mode,
        "current_draft_tail": current_draft_tail,
        "smart_context_recent_summaries": smart_context_recent_summaries,
        "smart_context_recent_full": smart_context_recent_full,
        "smart_context_story_skeleton": smart_context_story_skeleton,
        "detailed_outline_context": detailed_outline_context,
    }
    values["user"] = {"instruction": instruction, "requirements": requirements_obj}
    return values, requirements_obj


def build_chapter_generate_render_values(
    db: Session,
    *,
    project: Project,
    chapter: Chapter,
    body: ChapterGenerateRequest,
    user_id: str,
) -> tuple[dict[str, object], str, dict[str, object], dict[str, object]]:
    world_setting, style_guide, constraints, outline_text, characters_text, entries_text = _load_project_story_text_context(
        db,
        project_id=chapter.project_id,
        outline_id=chapter.outline_id,
        ctx=body.context,
    )
    resolved_style_guide, style_resolution = resolve_style_guide(
        db,
        project_id=chapter.project_id,
        user_id=user_id,
        requested_style_id=body.style_id,
        include_style_guide=bool(body.context.include_style_guide),
        settings_style_guide=style_guide,
    )

    prev_text, prev_ending = load_previous_chapter_context(
        db,
        project_id=chapter.project_id,
        outline_id=chapter.outline_id,
        chapter_number=int(chapter.number),
        previous_chapter=body.context.previous_chapter,
    )

    current_draft_tail = ""
    if body.mode == "append":
        current_draft_tail = resolve_current_draft_tail(chapter=chapter, request_tail=body.context.current_draft_tail)

    smart_recent_summaries = ""
    smart_recent_full = ""
    smart_story_skeleton = ""
    if body.context.include_smart_context:
        smart_recent_summaries, smart_recent_full, smart_story_skeleton = build_smart_context(
            db,
            project_id=chapter.project_id,
            outline_id=chapter.outline_id,
            chapter_number=int(chapter.number),
        )

    detailed_outline_ctx = load_detailed_outline_context(
        chapter_number=int(chapter.number),
        outline_id=chapter.outline_id,
        db=db,
    )

    base_instruction = body.instruction.strip()
    instruction = _format_chapter_generate_instruction(mode=body.mode, base_instruction=base_instruction)

    values, requirements_obj = assemble_chapter_generate_render_values(
        project=project,
        mode=body.mode,
        chapter_number=int(chapter.number),
        chapter_title=(chapter.title or ""),
        chapter_plan=(chapter.plan or ""),
        world_setting=world_setting,
        style_guide=resolved_style_guide,
        constraints=constraints,
        characters_text=characters_text,
        entries_text=entries_text,
        outline_text=outline_text,
        instruction=instruction,
        target_word_count=body.target_word_count,
        previous_chapter=prev_text,
        previous_chapter_ending=prev_ending,
        current_draft_tail=current_draft_tail,
        smart_context_recent_summaries=smart_recent_summaries,
        smart_context_recent_full=smart_recent_full,
        smart_context_story_skeleton=smart_story_skeleton,
        detailed_outline_context=detailed_outline_ctx,
    )

    return values, base_instruction, requirements_obj, style_resolution


def inject_plan_into_render_values(render_values: dict[str, object], *, plan_text: str) -> dict[str, object]:
    if not plan_text.strip():
        return render_values

    instruction_with_plan = f"{str(render_values.get('instruction') or '').rstrip()}\n\n<PLAN>\n{plan_text}\n</PLAN>"
    next_values = dict(render_values)
    next_values["instruction"] = instruction_with_plan
    next_values["story_plan"] = plan_text

    story_ns = next_values.get("story")
    if isinstance(story_ns, dict):
        story2 = dict(story_ns)
        story2["plan"] = plan_text
        next_values["story"] = story2
    else:
        next_values["story"] = {"plan": plan_text}

    user_ns = next_values.get("user")
    if isinstance(user_ns, dict):
        user2 = dict(user_ns)
        user2["instruction"] = instruction_with_plan
        next_values["user"] = user2

    return next_values


def build_post_edit_render_values(render_values: dict[str, object], *, raw_content: str) -> dict[str, object]:
    next_values = dict(render_values)
    next_values["raw_content"] = raw_content

    story_ns = next_values.get("story")
    if isinstance(story_ns, dict):
        story2 = dict(story_ns)
        story2["raw_content"] = raw_content
        next_values["story"] = story2
    else:
        next_values["story"] = {"raw_content": raw_content}

    return next_values
