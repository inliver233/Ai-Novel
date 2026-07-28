"""Shared chapter numbering primitives (backend-generation#5).

细纲卷 → 全局章节编号的唯一事实源。前置卷不可计数（structure 缺失 / 解析
失败 / 无正编号章节）时 fail-closed 抛 409，禁止静默 offset=0 造成乱序编号
冲突与跨卷误删。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.logging import log_event
from app.models.detailed_outline import DetailedOutline

logger = logging.getLogger("ainovel")


def extract_positive_chapter_numbers(chapters: Any) -> list[int]:
    if not isinstance(chapters, list):
        return []

    numbers: list[int] = []
    for item in chapters:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("number", 0))
        except (TypeError, ValueError):
            continue
        if number > 0:
            numbers.append(number)
    return numbers


def compute_chapter_offset(db: Session, detailed_outline: DetailedOutline) -> int:
    """计算当前卷的章节编号偏移量（前置卷章节数之和）。

    任何前置卷不可计数即拒绝（EARLIER_VOLUME_NO_CHAPTERS, 409）：
    先补齐前置卷骨架再生成后续卷。
    """
    earlier_volumes = (
        db.execute(
            select(DetailedOutline)
            .where(DetailedOutline.outline_id == detailed_outline.outline_id)
            .where(DetailedOutline.volume_number < detailed_outline.volume_number)
            .order_by(DetailedOutline.volume_number)
        )
        .scalars()
        .all()
    )

    offset = 0
    blocked: list[int] = []
    for volume in earlier_volumes:
        countable = 0
        if volume.structure_json:
            try:
                structure = json.loads(volume.structure_json)
                chapters = structure.get("chapters") if isinstance(structure, dict) else None
                countable = len(extract_positive_chapter_numbers(chapters))
            except Exception:
                countable = 0
        if countable <= 0:
            blocked.append(int(volume.volume_number))
            continue
        offset += countable

    if blocked:
        log_event(
            logger,
            "warning",
            event="CHAPTER_NUMBERING",
            action="offset_blocked",
            outline_id=str(detailed_outline.outline_id),
            volume_number=int(detailed_outline.volume_number),
            earlier_volume_numbers=blocked,
        )
        raise AppError(
            code="EARLIER_VOLUME_NO_CHAPTERS",
            message="前置卷尚无可计数章节，请先生成前置卷的章节骨架再生成本卷",
            status_code=409,
            details={"earlier_volume_numbers": blocked},
        )
    return offset
