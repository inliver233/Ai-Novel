from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_chapter_editor, require_project_editor
from app.core.config import settings
from app.core.errors import AppError
from app.models.batch_generation_task import BatchGenerationTask
from app.models.chapter import Chapter
from app.schemas.batch_generation import BatchGenerationCreateRequest
from app.services.batch_generation_commands import (
    BatchGenerationSnapshot,
    create_admitted_batch_generation_task,
    load_batch_generation_snapshot,
)
from app.services.batch_generation_quota import (
    enter_batch_generation_quota_admission,
    lock_and_enforce_batch_generation_quotas,
)
from app.services.llm_task_preset_resolver import resolve_task_runtime_provider
from app.services.outline_store import ensure_active_outline


def create_batch_generation_task(
    db: Session,
    *,
    user_id: str,
    project_id: str,
    body: BatchGenerationCreateRequest,
    request_id: str | None,
) -> BatchGenerationSnapshot:
    project = require_project_editor(db, project_id=project_id, user_id=user_id)
    resolve_task_runtime_provider(db, project_id=project_id, task_key="chapter_generate")

    max_count = int(settings.batch_generation_max_count or 200)
    if int(body.count) > max_count:
        raise AppError.validation(
            message=f"批量生成数量不能超过 {max_count}",
            details={"max_count": max_count},
        )

    if body.after_chapter_id:
        after = require_chapter_editor(db, chapter_id=body.after_chapter_id, user_id=user_id)
        if after.project_id != project_id:
            raise AppError.validation(message="起始章节（after_chapter_id）不属于当前项目")
        outline_id = after.outline_id
        start_number = int(after.number) + 1
    else:
        outline_id = ensure_active_outline(db, project=project).id
        start_number = 1

    limit = max(int(body.count) * 5, int(body.count))
    candidates = db.execute(
        select(Chapter)
        .where(
            Chapter.project_id == project_id,
            Chapter.outline_id == outline_id,
            Chapter.number >= start_number,
        )
        .order_by(Chapter.number.asc())
        .limit(limit)
    ).scalars().all()

    def _is_empty(chapter: Chapter) -> bool:
        return not ((chapter.content_md or "").strip() or (chapter.summary or "").strip())

    selected: list[Chapter] = []
    for chapter in candidates:
        if not body.include_existing and not _is_empty(chapter):
            continue
        selected.append(chapter)
        if len(selected) >= int(body.count):
            break

    if not selected:
        raise AppError.validation(message="没有可生成的章节（请先创建章节，或开启 include_existing）")
    if len(selected) < int(body.count):
        raise AppError.validation(
            message=f"目标章节不足：仅找到 {len(selected)} 章可生成，请减少数量或开启 include_existing",
            details={"found": len(selected), "required": int(body.count)},
        )

    if body.context.require_sequential:
        selected_numbers = {int(chapter.number) for chapter in selected}
        max_number = max(selected_numbers)
        if max_number > 1:
            previous_rows = db.execute(
                select(Chapter.number, Chapter.content_md, Chapter.summary).where(
                    Chapter.project_id == project_id,
                    Chapter.outline_id == outline_id,
                    Chapter.number < max_number,
                )
            ).all()
            existing = {int(row[0]): (row[1], row[2]) for row in previous_rows}
            missing_numbers: list[int] = []
            for number in range(1, max_number):
                if number in selected_numbers:
                    continue
                content_md, summary = existing.get(number, (None, None))
                if not ((content_md or "").strip() or (summary or "").strip()):
                    missing_numbers.append(number)
            if missing_numbers:
                raise AppError(
                    code="CHAPTER_PREREQ_MISSING",
                    message=f"缺少前置章节内容：第 {', '.join(str(number) for number in missing_numbers)} 章",
                    status_code=400,
                    details={"missing_numbers": missing_numbers},
                )

    selected_refs = [(str(chapter.id), int(chapter.number)) for chapter in selected]
    enter_batch_generation_quota_admission(db)
    require_project_editor(db, project_id=project_id, user_id=user_id)
    provider = resolve_task_runtime_provider(db, project_id=project_id, task_key="chapter_generate")
    selected_ids = [chapter_id for chapter_id, _number in selected_refs]
    locked_selected = db.execute(
        select(Chapter)
        .where(
            Chapter.id.in_(selected_ids),
            Chapter.project_id == project_id,
            Chapter.outline_id == outline_id,
        )
        .order_by(Chapter.number.asc())
        .with_for_update()
    ).scalars().all()
    if [(str(chapter.id), int(chapter.number)) for chapter in locked_selected] != selected_refs:
        raise AppError.conflict(
            message="批量生成目标章节在创建过程中发生变化，请重试",
            details={"reason": "chapter_selection_changed"},
        )
    if not body.include_existing and any(not _is_empty(chapter) for chapter in locked_selected):
        raise AppError.conflict(
            message="批量生成目标章节已有新内容，请重试",
            details={"reason": "chapter_content_changed"},
        )

    lock_and_enforce_batch_generation_quotas(
        db,
        project_id=project_id,
        user_id=user_id,
        provider=provider,
    )
    return create_admitted_batch_generation_task(
        db,
        project_id=project_id,
        outline_id=str(outline_id),
        actor_user_id=user_id,
        runtime_provider=provider,
        params_json=json.dumps(body.model_dump(), ensure_ascii=False),
        chapter_refs=selected_refs,
        request_id=request_id,
    )


def load_latest_batch_generation_snapshot(
    db: Session,
    *,
    project_id: str,
) -> BatchGenerationSnapshot | None:
    task_id = db.execute(
        select(BatchGenerationTask.id)
        .where(BatchGenerationTask.project_id == project_id)
        .order_by(BatchGenerationTask.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if task_id is None:
        return None
    return load_batch_generation_snapshot(db, task_id=str(task_id))
