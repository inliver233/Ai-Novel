from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.utils import new_id
from app.llm.capabilities import max_context_tokens_limit
from app.llm.client import call_llm_stream_messages
from app.llm.messages import ChatMessage
from app.models.chapter import Chapter
from app.models.detailed_outline import DetailedOutline
from app.models.outline import Outline
from app.models.project import Project
from app.services.chapter_numbering import compute_chapter_offset, extract_positive_chapter_numbers
from app.services.chapter_skeleton_generation.models import ChapterSkeletonResult
from app.services.chapter_skeleton_generation.parse_service import parse_chapter_skeleton_output
from app.services.chapter_skeleton_generation.prepare_service import prepare_chapter_skeleton_render_values
from app.services.generation_service import PreparedLlmCall, with_param_overrides
from app.services.prompt_budget import estimate_tokens
from app.services.prompt_presets import render_preset_for_task
from app.services.run_store import write_generation_run
from app.utils.sse_response import (
    sse_chunk,
    sse_done,
    sse_error,
    sse_heartbeat,
    sse_progress,
    sse_result,
)

logger = logging.getLogger("ainovel")

DEFAULT_SYSTEM_PROMPT = """你是一位专业的小说章节规划师。根据提供的大纲和细纲内容，为当前卷生成详细的章节骨架。

要求：
1. 根据细纲内容合理划分章节
2. 每个章节包含：编号(number)、标题(title)、摘要(summary)、关键节拍(beats)
3. 章节之间要有合理的起承转合和情节推进
4. 注意与前后卷的内容衔接

输出格式（严格JSON）：
{"chapters": [{"number": 1, "title": "章节标题", "summary": "章节摘要（100-200字）", "beats": ["节拍1", "节拍2"]}]}"""

DEFAULT_USER_PROMPT_TEMPLATE = """请基于以下信息生成当前卷的章节骨架。

项目信息：
- 项目名：{project_name}
- 类型：{genre}
- 一句话梗概：{logline}

世界设定：
{world_setting}

风格指南：
{style_guide}

硬性约束：
{constraints}

角色信息：
{characters}

总大纲：
{outline}

当前卷信息：
- 卷号：{volume_number}
- 卷标题：{volume_title}

当前卷细纲正文：
{detailed_outline_content}

当前卷细纲结构（JSON）：
{detailed_outline_structure}

前一卷摘要：
{previous_volume_summary}

后一卷摘要：
{next_volume_summary}

期望章节数：
{chapters_count}

补充指令：
{instruction}

请严格输出 JSON，且顶层结构必须为 {{"chapters": [...]}}。"""


class _SafeFormatDict(dict[str, object]):
    def __missing__(self, key: str) -> str:
        return ""


def generate_chapter_skeleton_stream_events(
    *,
    request_id: str,
    detailed_outline: DetailedOutline,
    outline: Outline,
    project: Project,
    llm_call: PreparedLlmCall,
    api_key: str,
    user_id: str,
    db: Session,
    neighbor_summaries: dict[str, str] | None = None,
    chapters_count: int | None = None,
    instruction: str | None = None,
    context_flags: dict[str, Any] | None = None,
    replace_chapters: bool = True,
):
    yield sse_progress(message="准备生成章节骨架...", progress=0)

    raw_output = ""
    generation_run_id: str | None = None
    stream_run_written = False
    generation_started = False

    render_values = prepare_chapter_skeleton_render_values(
        detailed_outline,
        outline,
        project,
        db,
        neighbor_summaries=neighbor_summaries,
        chapters_count=chapters_count,
        instruction=instruction,
        context_flags=context_flags,
    )

    prompt_system = ""
    prompt_user = ""
    prompt_messages: list[ChatMessage] = []
    prompt_render_log_json: str | None = None

    try:
        try:
            prompt_system, prompt_user, prompt_messages, _, _, _, render_log = render_preset_for_task(
                db,
                project_id=project.id,
                task="chapter_skeleton_generate",
                values=render_values,
                macro_seed=request_id,
                provider=llm_call.provider,
                allow_autocreate=False,
            )
            if _prompt_is_empty(prompt_system, prompt_user, prompt_messages):
                raise AppError.validation("章节骨架 prompt 渲染为空")
            if not prompt_messages:
                prompt_messages = _build_prompt_messages(prompt_system, prompt_user)
        except AppError:
            prompt_system, prompt_user, prompt_messages, render_log = _render_default_prompt(render_values)

        prompt_render_log_json = json.dumps(render_log, ensure_ascii=False)

        current_max_tokens = llm_call.params.get("max_tokens")
        prompt_tokens = (
            sum(estimate_tokens(str(message.content or "")) for message in prompt_messages)
            if prompt_messages
            else estimate_tokens(prompt_system) + estimate_tokens(prompt_user)
        )
        ctx_limit = max_context_tokens_limit(llm_call.provider, llm_call.model)
        if isinstance(ctx_limit, int) and ctx_limit > 0:
            safe_max = max(4096, ctx_limit - prompt_tokens - 512)
            if current_max_tokens is None or (
                isinstance(current_max_tokens, int) and current_max_tokens > safe_max
            ):
                llm_call = with_param_overrides(llm_call, {"max_tokens": safe_max})
        elif current_max_tokens is None:
            llm_call = with_param_overrides(llm_call, {"max_tokens": 16000})

        # 编号偏移在 LLM 调用前 fail-fast：前置卷缺章直接 409，不烧 token。
        chapter_offset = compute_chapter_offset(db, detailed_outline)

        yield sse_progress(message="调用模型...", progress=10)
        generation_started = True

        # 阻塞流式 LLM 前结束读事务，释放连接（expire_on_commit=False）。
        db.commit()
        stream_iter, state = call_llm_stream_messages(
            provider=llm_call.provider,
            base_url=llm_call.base_url,
            model=llm_call.model,
            api_key=api_key,
            messages=prompt_messages,
            params=llm_call.params,
            timeout_seconds=llm_call.timeout_seconds,
            extra=llm_call.extra,
        )

        last_progress = 10
        last_progress_ts = 0.0
        chunk_count = 0
        try:
            for delta in stream_iter:
                raw_output += delta
                yield sse_chunk(delta)
                chunk_count += 1
                if chunk_count % 12 == 0:
                    yield sse_heartbeat()
                now = time.monotonic()
                if now - last_progress_ts >= 0.8:
                    next_progress = 10 + int(min(1.0, len(raw_output) / 6000.0) * 80)
                    next_progress = max(last_progress, min(90, next_progress))
                    if next_progress != last_progress:
                        last_progress = next_progress
                        yield sse_progress(message="生成中...", progress=next_progress)
                    last_progress_ts = now
        finally:
            close = getattr(stream_iter, "close", None)
            if callable(close):
                close()

        generation_run_id = write_generation_run(
            request_id=request_id,
            actor_user_id=user_id,
            project_id=project.id,
            chapter_id=None,
            run_type="chapter_skeleton_stream",
            provider=llm_call.provider,
            model=llm_call.model,
            prompt_system=prompt_system,
            prompt_user=prompt_user,
            prompt_render_log_json=prompt_render_log_json,
            params_json=llm_call.params_json,
            output_text=raw_output,
            error_json=None,
        )
        stream_run_written = True

        yield sse_progress(message="解析输出...", progress=90)
        content_md, chapters, warnings, parse_error = parse_chapter_skeleton_output(raw_output)
        if parse_error is not None:
            yield sse_error(
                error=str(parse_error.get("message") or "章节骨架解析失败"),
                code=422,
            )
            yield sse_done()
            return

        # 合并 chapters 到现有 structure_json，保留原始细纲内容
        existing_structure: dict[str, Any] = {}
        if detailed_outline.structure_json:
            try:
                parsed = json.loads(detailed_outline.structure_json)
                if isinstance(parsed, dict):
                    existing_structure = parsed
            except Exception:
                pass

        previous_raw = extract_positive_chapter_numbers(existing_structure.get("chapters"))
        previous_chapter_numbers = {chapter_offset + (i + 1) for i in range(len(previous_raw))}
        existing_structure["chapters"] = chapters
        detailed_outline.structure_json = json.dumps(existing_structure, ensure_ascii=False)
        # 不覆写 content_md — 保留原始细纲内容

        new_raw = extract_positive_chapter_numbers(chapters)
        new_chapter_numbers = {chapter_offset + (i + 1) for i in range(len(new_raw))}
        created_chapters = _create_chapter_records(
            db,
            detailed_outline,
            chapters,
            replace=replace_chapters,
            chapter_offset=chapter_offset,
            replace_numbers=previous_chapter_numbers | new_chapter_numbers,
        )
        if generation_run_id is None:
            raise AppError(
                code="CHAPTER_SKELETON_RUN_MISSING",
                message="章节骨架生成记录缺失",
                status_code=500,
            )

        result = ChapterSkeletonResult(
            detailed_outline_id=detailed_outline.id,
            volume_number=detailed_outline.volume_number,
            volume_title=detailed_outline.volume_title or "",
            content_md=content_md,
            chapters=chapters,
            chapter_count=len(chapters),
            run_id=generation_run_id,
            warnings=warnings,
        )
        result_data = asdict(result)
        result_data["created_chapters"] = created_chapters
        result_data["generation_run_id"] = generation_run_id
        result_data["finish_reason"] = state.finish_reason
        result_data["latency_ms"] = state.latency_ms
        if state.dropped_params:
            result_data["dropped_params"] = state.dropped_params

        yield sse_progress(message="完成", progress=100, status="success")
        yield sse_result(result_data)
        yield sse_done()
    except GeneratorExit:
        return
    except AppError as exc:
        db.rollback()
        if generation_started and not stream_run_written:
            write_generation_run(
                request_id=request_id,
                actor_user_id=user_id,
                project_id=project.id,
                chapter_id=None,
                run_type="chapter_skeleton_stream",
                provider=llm_call.provider,
                model=llm_call.model,
                prompt_system=prompt_system,
                prompt_user=prompt_user,
                prompt_render_log_json=prompt_render_log_json,
                params_json=llm_call.params_json,
                output_text=raw_output or None,
                error_json=json.dumps(
                    {"code": exc.code, "message": exc.message, "details": exc.details},
                    ensure_ascii=False,
                ),
            )
        yield sse_error(error=f"{exc.message} ({exc.code})", code=exc.status_code)
        yield sse_done()
    except Exception:
        db.rollback()
        logger.exception(
            "chapter_skeleton_stream_error detailed_outline_id=%s request_id=%s",
            detailed_outline.id,
            request_id,
        )
        if generation_started and not stream_run_written:
            write_generation_run(
                request_id=request_id,
                actor_user_id=user_id,
                project_id=project.id,
                chapter_id=None,
                run_type="chapter_skeleton_stream",
                provider=llm_call.provider,
                model=llm_call.model,
                prompt_system=prompt_system,
                prompt_user=prompt_user,
                prompt_render_log_json=prompt_render_log_json,
                params_json=llm_call.params_json,
                output_text=raw_output or None,
                error_json=json.dumps(
                    {"code": "INTERNAL_ERROR", "message": "章节骨架生成失败"},
                    ensure_ascii=False,
                ),
            )
        yield sse_error(error="章节骨架生成失败", code=500)
        yield sse_done()


def _prompt_is_empty(prompt_system: str, prompt_user: str, prompt_messages: list[ChatMessage]) -> bool:
    if prompt_system.strip() or prompt_user.strip():
        return False
    return not any(str(msg.content or "").strip() for msg in prompt_messages)


def _render_default_prompt(
    render_values: dict[str, object],
) -> tuple[str, str, list[ChatMessage], dict[str, Any]]:
    prompt_system = DEFAULT_SYSTEM_PROMPT
    prompt_user = DEFAULT_USER_PROMPT_TEMPLATE.format_map(_SafeFormatDict(render_values))
    prompt_messages = _build_prompt_messages(prompt_system, prompt_user)
    render_log: dict[str, Any] = {
        "task": "chapter_skeleton_generate",
        "source": "builtin_default",
        "used_default_prompt": True,
    }
    return prompt_system, prompt_user, prompt_messages, render_log


def _build_prompt_messages(prompt_system: str, prompt_user: str) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    if prompt_system.strip():
        messages.append(ChatMessage(role="system", content=prompt_system))
    if prompt_user.strip():
        messages.append(ChatMessage(role="user", content=prompt_user))
    return messages


def _create_chapter_records(
    db: Session,
    detailed_outline: DetailedOutline,
    chapters: list[dict[str, Any]],
    *,
    replace: bool = True,
    chapter_offset: int = 0,
    replace_numbers: set[int] | None = None,
) -> list[dict[str, Any]]:
    existing_numbers: set[int] = set()
    # Sort and enumerate to get normalized numbers
    sorted_chs = sorted(
        [ch for ch in chapters if isinstance(ch, dict) and int(ch.get("number", 0) or 0) > 0],
        key=lambda c: int(c.get("number", 0)),
    )
    new_numbers = {chapter_offset + (i + 1) for i in range(len(sorted_chs))}

    numbers_to_replace = replace_numbers if replace_numbers is not None else new_numbers

    if replace and numbers_to_replace:
        existing = (
            db.execute(
                select(Chapter)
                .where(Chapter.outline_id == detailed_outline.outline_id)
                .where(Chapter.project_id == detailed_outline.project_id)
                .where(Chapter.number.in_(numbers_to_replace))
            )
            .scalars()
            .all()
        )
        for row in existing:
            db.delete(row)
        db.flush()
    elif not replace and new_numbers:
        existing_numbers = {
            row[0]
            for row in db.execute(
                select(Chapter.number)
                .where(Chapter.outline_id == detailed_outline.outline_id)
                .where(Chapter.project_id == detailed_outline.project_id)
                .where(Chapter.number.in_(new_numbers))
            ).all()
        }

    # Sort chapters by raw number and use sequential local numbering
    sorted_items = sorted(
        [item for item in chapters if isinstance(item, dict)],
        key=lambda c: int(c.get("number", 0)) if isinstance(c.get("number"), (int, float, str)) else 0,
    )

    created: list[dict[str, Any]] = []
    local_idx = 0
    for item in sorted_items:
        try:
            raw_number = int(item.get("number", 0))
        except (TypeError, ValueError):
            continue
        if raw_number <= 0:
            continue
        local_idx += 1
        number = chapter_offset + local_idx
        if not replace and number in existing_numbers:
            continue

        title = str(item.get("title") or "")
        summary = str(item.get("summary") or "").strip()
        beats_raw = item.get("beats") or []
        beats: list[str] = []
        if isinstance(beats_raw, list):
            beats = [str(beat).strip() for beat in beats_raw if str(beat).strip()]
        elif str(beats_raw).strip():
            beats = [str(beats_raw).strip()]

        plan_parts: list[str] = []
        if summary:
            plan_parts.append(summary)
        if beats:
            plan_parts.append("\n".join(f"- {beat}" for beat in beats))
        plan = "\n\n".join(plan_parts)

        chapter = Chapter(
            id=new_id(),
            project_id=detailed_outline.project_id,
            outline_id=detailed_outline.outline_id,
            number=number,
            title=title,
            plan=plan,
            status="planned",
        )
        db.add(chapter)
        created.append(
            {
                "id": chapter.id,
                "number": number,
                "title": title,
                "plan": plan,
            }
        )

    db.commit()
    return created
