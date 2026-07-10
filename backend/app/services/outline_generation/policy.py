from __future__ import annotations

from app.llm.capabilities import max_output_tokens_limit

OUTLINE_FILL_MIN_BATCH_SIZE = 6
OUTLINE_FILL_MAX_BATCH_SIZE = 18
OUTLINE_FILL_STAGNANT_ROUNDS_LIMIT = 3
OUTLINE_FILL_MAX_TOTAL_ATTEMPTS = 48
OUTLINE_FILL_HEARTBEAT_INTERVAL_SECONDS = 1.0
OUTLINE_FILL_POLL_INTERVAL_SECONDS = 0.2
OUTLINE_GAP_REPAIR_MAX_MISSING = 120
OUTLINE_GAP_REPAIR_BATCH_SIZE = 4
OUTLINE_GAP_REPAIR_STAGNANT_LIMIT = 4
OUTLINE_GAP_REPAIR_FINAL_SWEEP_MAX_MISSING = 36
OUTLINE_GAP_REPAIR_FINAL_SWEEP_ATTEMPTS_PER_CHAPTER = 3
OUTLINE_SEGMENT_TRIGGER_CHAPTER_COUNT = 80
OUTLINE_SEGMENT_MIN_BATCH_SIZE = 6
OUTLINE_SEGMENT_MAX_BATCH_SIZE = 12
OUTLINE_SEGMENT_DEFAULT_BATCH_SIZE = 10
OUTLINE_SEGMENT_MAX_ATTEMPTS_PER_BATCH = 6
OUTLINE_SEGMENT_STAGNANT_ATTEMPTS_LIMIT = 3
OUTLINE_SEGMENT_RECENT_CONTEXT_WINDOW = 24
OUTLINE_SEGMENT_INDEX_MAX_ITEMS = 140
OUTLINE_SEGMENT_INDEX_MAX_CHARS = 6000
OUTLINE_SEGMENT_RECENT_WINDOW_MAX_CHARS = 2800
OUTLINE_STREAM_RAW_PREVIEW_MAX_CHARS = 1800


def _extract_target_chapter_count(requirements: dict[str, object] | None) -> int | None:
    if not isinstance(requirements, dict):
        return None
    raw = requirements.get("chapter_count")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return None
            value = int(text)
        else:
            value = int(raw)
    except Exception:
        return None
    if value <= 0:
        return None
    return min(value, 2000)


def _build_outline_generation_guidance(target_chapter_count: int | None) -> dict[str, str]:
    if not target_chapter_count:
        return {
            "chapter_count_rule": "",
            "chapter_detail_rule": "beats 每章 5~9 条，按发生顺序；每条用短句，明确“发生了什么/造成什么后果”。",
        }
    if target_chapter_count <= 20:
        detail = "beats 每章 5~9 条，按发生顺序；每条用短句，明确“发生了什么/造成什么后果”。"
    elif target_chapter_count <= 40:
        detail = "beats 每章 2~4 条，保持因果推进；每条保持短句，避免冗长。"
    elif target_chapter_count <= 80:
        detail = "beats 每章 1~2 条，仅保留关键推进；优先保证章号覆盖完整。"
    elif target_chapter_count <= 120:
        detail = "beats 每章 1~2 条，只保留主冲突与关键转折，保证节奏连续。"
    else:
        detail = "beats 每章 1 条，极简表达关键推进；若长度受限，优先保留章节覆盖与编号完整。"
    return {
        "chapter_count_rule": (
            f"chapters 必须输出 {target_chapter_count} 章，number 需完整覆盖 1..{target_chapter_count} 且不缺号。"
        ),
        "chapter_detail_rule": detail,
    }


def _recommend_outline_max_tokens(
    *,
    target_chapter_count: int | None,
    provider: str,
    model: str | None,
    current_max_tokens: int | None,
) -> int | None:
    """Ensure max_tokens is sufficient for the target chapter count.

    Only *raises* max_tokens when it seems too low — never lowers a value the
    user already configured via LLM profile.
    """
    if not target_chapter_count or target_chapter_count <= 20:
        return None
    # ~200 tokens per chapter is a conservative floor for outline JSON.
    floor = min(target_chapter_count * 200, 64000)
    if isinstance(current_max_tokens, int) and current_max_tokens >= floor:
        return None  # already sufficient
    limit = max_output_tokens_limit(provider, model)
    wanted = floor
    if isinstance(limit, int) and limit > 0:
        wanted = min(wanted, int(limit))
    if isinstance(current_max_tokens, int) and current_max_tokens >= wanted:
        return None
    return wanted if wanted > 0 else None


def _should_use_outline_segmented_mode(target_chapter_count: int | None) -> bool:
    return bool(target_chapter_count and target_chapter_count >= OUTLINE_SEGMENT_TRIGGER_CHAPTER_COUNT)


def _outline_segment_batch_size_for_target(target_chapter_count: int) -> int:
    if target_chapter_count <= 120:
        return OUTLINE_SEGMENT_MAX_BATCH_SIZE
    if target_chapter_count <= 500:
        return OUTLINE_SEGMENT_DEFAULT_BATCH_SIZE
    return max(OUTLINE_SEGMENT_MIN_BATCH_SIZE, OUTLINE_SEGMENT_DEFAULT_BATCH_SIZE - 2)


def _outline_segment_max_attempts_for_batch(requested_count: int) -> int:
    if requested_count <= 0:
        return 1
    estimated = max(3, ((requested_count + 2) // 3) + 1)
    return min(OUTLINE_SEGMENT_MAX_ATTEMPTS_PER_BATCH, estimated)


def _outline_segment_batches(target_chapter_count: int, batch_size: int) -> list[list[int]]:
    size = max(OUTLINE_SEGMENT_MIN_BATCH_SIZE, min(OUTLINE_SEGMENT_MAX_BATCH_SIZE, int(batch_size)))
    out: list[list[int]] = []
    start = 1
    while start <= target_chapter_count:
        end = min(target_chapter_count, start + size - 1)
        out.append(list(range(start, end + 1)))
        start = end + 1
    return out


def _recommend_outline_segment_max_tokens(
    *,
    requested_count: int,
    provider: str,
    model: str | None,
    current_max_tokens: int | None,
) -> int | None:
    """Floor for segment generation — only raises, never lowers."""
    if requested_count <= 0:
        return None
    floor = max(2000, requested_count * 250)
    if isinstance(current_max_tokens, int) and current_max_tokens >= floor:
        return None
    limit = max_output_tokens_limit(provider, model)
    wanted = floor
    if isinstance(limit, int) and limit > 0:
        wanted = min(wanted, int(limit))
    if isinstance(current_max_tokens, int) and current_max_tokens >= wanted:
        return None
    return wanted if wanted > 0 else None


def _outline_fill_batch_size_for_missing(missing_count: int) -> int:
    if missing_count <= 0:
        return OUTLINE_FILL_MIN_BATCH_SIZE
    if missing_count >= 160:
        return OUTLINE_FILL_MAX_BATCH_SIZE
    if missing_count >= 80:
        return 14
    if missing_count >= 40:
        return 12
    if missing_count >= 20:
        return 10
    if missing_count >= 10:
        return 8
    return OUTLINE_FILL_MIN_BATCH_SIZE


def _outline_fill_max_attempts_for_missing(missing_count: int) -> int:
    if missing_count <= 0:
        return 1
    estimated = (missing_count + 4) // 5 + 2
    return max(6, min(OUTLINE_FILL_MAX_TOTAL_ATTEMPTS, estimated))


def _outline_fill_progress_message(progress: dict[str, object] | None) -> str:
    if not isinstance(progress, dict):
        return "补全缺失章节..."
    event = str(progress.get("event") or "")
    remaining_raw = progress.get("remaining_count")
    remaining = int(remaining_raw) if isinstance(remaining_raw, int) else 0
    attempt_raw = progress.get("attempt")
    attempt = int(attempt_raw) if isinstance(attempt_raw, int) else 0
    max_attempts_raw = progress.get("max_attempts")
    max_attempts = int(max_attempts_raw) if isinstance(max_attempts_raw, int) else 0
    if event.startswith("gap_repair"):
        if event == "gap_repair_final_sweep_start":
            return f"终检兜底启动：剩余 {remaining} 章"
        if event == "gap_repair_final_sweep_attempt_start":
            return f"终检兜底中... 第 {attempt}/{max_attempts} 轮，剩余 {remaining} 章"
        if event == "gap_repair_final_sweep_applied":
            return f"终检兜底已插入，剩余 {remaining} 章"
        if event == "gap_repair_final_sweep_done":
            return f"终检兜底结束，仍缺 {remaining} 章" if remaining > 0 else "终检兜底完成，章节已齐全"
        if event == "gap_repair_start":
            return f"终检补全启动：剩余 {remaining} 章待修复"
        if event == "gap_repair_attempt_start":
            return f"终检补全中... 第 {attempt}/{max_attempts} 轮，剩余 {remaining} 章"
        if event == "gap_repair_applied":
            return f"终检补全已应用，剩余 {remaining} 章"
        if event == "gap_repair_done":
            return f"终检补全结束，仍缺 {remaining} 章" if remaining > 0 else "终检补全完成，章节已齐全"
    if attempt > 0 and max_attempts > 0 and remaining > 0:
        return f"补全缺失章节... 第 {attempt}/{max_attempts} 轮，剩余 {remaining} 章"
    if remaining > 0:
        return f"补全缺失章节... 剩余 {remaining} 章"
    return "补全缺失章节..."


def _outline_segment_progress_message(progress: dict[str, object] | None) -> str:
    if not isinstance(progress, dict):
        return "长篇分段生成中..."
    event = str(progress.get("event") or "")
    if event.startswith("fill_"):
        mapped = dict(progress)
        mapped["event"] = event.removeprefix("fill_")
        return _outline_fill_progress_message(mapped)

    batch_index = int(progress.get("batch_index") or 0)
    batch_count = int(progress.get("batch_count") or 0)
    range_text = str(progress.get("range") or "")
    attempt = int(progress.get("attempt") or 0)
    max_attempts = int(progress.get("max_attempts") or 0)
    completed = int(progress.get("completed_count") or 0)
    target = int(progress.get("target_chapter_count") or 0)
    remaining = int(progress.get("remaining_count") or 0)

    if event == "segment_start":
        return f"长篇分段生成启动：共 {batch_count} 批"
    if event == "batch_attempt_start":
        return f"分段生成 第 {batch_index}/{batch_count} 批（章号 {range_text}），尝试 {attempt}/{max_attempts}"
    if event == "batch_call_failed":
        return f"分段生成 第 {batch_index}/{batch_count} 批调用失败，自动重试（{attempt}/{max_attempts}）"
    if event == "batch_parse_failed":
        return f"分段生成 第 {batch_index}/{batch_count} 批解析失败，自动重试（{attempt}/{max_attempts}）"
    if event == "batch_no_progress":
        return f"分段生成 第 {batch_index}/{batch_count} 批无有效新章，自动重试（{attempt}/{max_attempts}）"
    if event == "batch_applied":
        return f"分段生成已完成 {completed}/{target} 章，剩余 {remaining} 章" if target > 0 else "分段生成已应用一批结果"
    if event == "batch_incomplete":
        return f"分段生成 第 {batch_index}/{batch_count} 批未完全收敛，剩余 {remaining} 章"
    if event == "segment_done":
        return "分段生成完成"
    if target > 0 and completed > 0:
        return f"分段生成中... 已完成 {completed}/{target} 章"
    return "长篇分段生成中..."


def _outline_gap_repair_max_attempts(missing_count: int) -> int:
    if missing_count <= 0:
        return 1
    estimated = missing_count * 2 + 2
    return max(8, min(OUTLINE_FILL_MAX_TOTAL_ATTEMPTS, estimated))
