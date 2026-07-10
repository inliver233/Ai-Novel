from __future__ import annotations

import json
import logging
from typing import Iterator

from fastapi import APIRouter, Header, Query, Request
from sqlalchemy import select

from app.api.deps import (
    DbDep,
    UserIdDep,
    require_outline_viewer,
    require_project_editor,
    require_project_viewer,
)
from app.core.errors import AppError, ok_payload
from app.db.utils import new_id
from app.models.chapter import Chapter
from app.models.detailed_outline import DetailedOutline
from app.models.outline import Outline
from app.models.project import Project
from app.schemas.detailed_outline import (
    ChapterSkeletonGenerateRequest,
    DetailedOutlineBatchCreateRequest,
    DetailedOutlineCreate,
    DetailedOutlineGenerateRequest,
    DetailedOutlineListItem,
    DetailedOutlineOut,
    DetailedOutlineUpdate,
)
from app.db.utils import utc_now
from app.services.detailed_outline_generation.app_service import (
    create_chapters_from_detailed_outline,
    extract_volumes_from_outline,
    generate_all_detailed_outlines,
    generate_detailed_outline_for_volume,
)
from app.services.chapter_skeleton_generation.stream_service import generate_chapter_skeleton_stream_events
from app.services.llm_task_preset_resolver import resolve_task_llm_config
from app.utils.sse_response import create_sse_response, format_sse, sse_done, sse_error

router = APIRouter()
logger = logging.getLogger("ainovel")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detailed_outline_out(row: DetailedOutline) -> dict:
    structure = None
    if row.structure_json:
        try:
            structure = json.loads(row.structure_json)
        except Exception:
            pass
    return DetailedOutlineOut(
        id=row.id,
        outline_id=row.outline_id,
        project_id=row.project_id,
        volume_number=row.volume_number,
        volume_title=row.volume_title,
        content_md=row.content_md,
        structure=structure,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    ).model_dump()


def _require_detailed_outline(db: DbDep, *, detailed_outline_id: str, user_id: str) -> DetailedOutline:
    row = db.get(DetailedOutline, detailed_outline_id)
    if row is None:
        raise AppError.not_found()
    require_project_viewer(db, project_id=row.project_id, user_id=user_id)
    return row


def _require_detailed_outline_editor(db: DbDep, *, detailed_outline_id: str, user_id: str) -> DetailedOutline:
    row = db.get(DetailedOutline, detailed_outline_id)
    if row is None:
        raise AppError.not_found()
    require_project_editor(db, project_id=row.project_id, user_id=user_id)
    return row


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/outlines/{outline_id}/detailed_outlines")
def list_detailed_outlines(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    outline_id: str,
) -> dict:
    request_id = request.state.request_id
    require_project_viewer(db, project_id=project_id, user_id=user_id)
    require_outline_viewer(db, outline_id=outline_id, user_id=user_id)

    rows = (
        db.execute(
            select(DetailedOutline)
            .where(DetailedOutline.outline_id == outline_id, DetailedOutline.project_id == project_id)
            .order_by(DetailedOutline.volume_number.asc())
        )
        .scalars()
        .all()
    )

    # Count chapters from each detailed outline's structure_json
    chapter_counts: dict[str, int] = {}
    for r in rows:
        count = 0
        if r.structure_json:
            try:
                s = json.loads(r.structure_json)
                if isinstance(s, dict):
                    chs = s.get("chapters")
                    if isinstance(chs, list):
                        count = len(chs)
            except Exception:
                pass
        chapter_counts[r.id] = count

    items = [
        DetailedOutlineListItem(
            id=r.id,
            outline_id=r.outline_id,
            volume_number=r.volume_number,
            volume_title=r.volume_title,
            status=r.status,
            chapter_count=chapter_counts.get(r.id, 0),
            updated_at=r.updated_at,
        ).model_dump()
        for r in rows
    ]
    return ok_payload(request_id=request_id, data={"detailed_outlines": items})


@router.post("/projects/{project_id}/outlines/{outline_id}/detailed_outlines")
def create_detailed_outline(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    outline_id: str,
    body: DetailedOutlineCreate,
) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)
    outline = require_outline_viewer(db, outline_id=outline_id, user_id=user_id)
    if outline.project_id != project_id:
        raise AppError.not_found()

    structure_json = json.dumps(body.structure, ensure_ascii=False) if body.structure is not None else None

    row = DetailedOutline(
        id=new_id(),
        outline_id=outline_id,
        project_id=project_id,
        volume_number=body.volume_number,
        volume_title=body.volume_title,
        content_md=body.content_md or "",
        structure_json=structure_json,
        status="planned",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return ok_payload(request_id=request_id, data={"detailed_outline": _detailed_outline_out(row)})


@router.get("/detailed_outlines/{detailed_outline_id}")
def get_detailed_outline(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    detailed_outline_id: str,
) -> dict:
    request_id = request.state.request_id
    row = _require_detailed_outline(db, detailed_outline_id=detailed_outline_id, user_id=user_id)
    return ok_payload(request_id=request_id, data={"detailed_outline": _detailed_outline_out(row)})


@router.put("/detailed_outlines/{detailed_outline_id}")
def update_detailed_outline(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    detailed_outline_id: str,
    body: DetailedOutlineUpdate,
) -> dict:
    request_id = request.state.request_id
    row = _require_detailed_outline_editor(db, detailed_outline_id=detailed_outline_id, user_id=user_id)

    if body.volume_title is not None:
        row.volume_title = body.volume_title
    if body.content_md is not None:
        row.content_md = body.content_md
    if body.structure is not None:
        row.structure_json = json.dumps(body.structure, ensure_ascii=False)
    if body.status is not None:
        row.status = body.status

    db.commit()
    db.refresh(row)
    return ok_payload(request_id=request_id, data={"detailed_outline": _detailed_outline_out(row)})


@router.delete("/detailed_outlines/{detailed_outline_id}")
def delete_detailed_outline(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    detailed_outline_id: str,
) -> dict:
    request_id = request.state.request_id
    row = _require_detailed_outline_editor(db, detailed_outline_id=detailed_outline_id, user_id=user_id)
    db.delete(row)
    db.commit()
    return ok_payload(request_id=request_id, data={"deleted": True})


# ---------------------------------------------------------------------------
# Batch create from parsed data (no LLM call)
# ---------------------------------------------------------------------------

@router.post("/projects/{project_id}/outlines/{outline_id}/detailed_outlines/batch")
def batch_create_detailed_outlines(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    outline_id: str,
    body: DetailedOutlineBatchCreateRequest,
) -> dict:
    """Create/update detailed outlines from pre-parsed data (e.g. from outline parsing agent).

    No LLM call is made -- the chapters structure is saved directly.
    Uses upsert: existing volumes (same outline_id + volume_number) are overwritten.
    """
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)
    require_outline_viewer(db, outline_id=outline_id, user_id=user_id)

    now = utc_now()
    created_ids: list[str] = []

    for item in body.detailed_outlines:
        # Build content_md from volume_summary + chapter summaries
        content_parts: list[str] = []
        if item.volume_summary:
            content_parts.append(item.volume_summary)
        for ch in item.chapters:
            if isinstance(ch, dict):
                ch_title = ch.get("title", "")
                ch_summary = ch.get("summary", "")
                if ch_title or ch_summary:
                    content_parts.append(f"### {ch.get('number', '?')}. {ch_title}\n{ch_summary}")
        content_md = "\n\n".join(content_parts)

        structure = {"chapters": item.chapters} if item.chapters else None
        structure_json = json.dumps(structure, ensure_ascii=False) if structure else None

        # Upsert: find existing by (outline_id, volume_number)
        existing = db.execute(
            select(DetailedOutline).where(
                DetailedOutline.outline_id == outline_id,
                DetailedOutline.volume_number == item.volume_number,
            )
        ).scalar_one_or_none()

        if existing is not None:
            existing.volume_title = item.volume_title or existing.volume_title
            existing.content_md = content_md
            existing.structure_json = structure_json
            existing.status = "done"
            existing.updated_at = now
            created_ids.append(existing.id)
        else:
            row = DetailedOutline(
                id=new_id(),
                outline_id=outline_id,
                project_id=project_id,
                volume_number=item.volume_number,
                volume_title=item.volume_title or "",
                content_md=content_md,
                structure_json=structure_json,
                status="done",
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            created_ids.append(row.id)

    db.commit()
    return ok_payload(request_id=request_id, data={"count": len(created_ids), "ids": created_ids})


# ---------------------------------------------------------------------------
# Generation endpoints (SSE streaming)
# ---------------------------------------------------------------------------

def _generate_all_sse_events(
    outline_id: str,
    project_id: str,
    user_id: str,
    request_id: str,
    db: DbDep,
    *,
    x_llm_api_key: str | None = None,
    chapters_per_volume: int | None = None,
    instruction: str | None = None,
    context_flags: dict | None = None,
) -> Iterator[str]:
    """Wrap generate_all_detailed_outlines dict events into SSE strings."""
    try:
        for event in generate_all_detailed_outlines(
            outline_id=outline_id,
            project_id=project_id,
            user_id=user_id,
            request_id=request_id,
            db=db,
            x_llm_api_key=x_llm_api_key,
            chapters_per_volume=chapters_per_volume,
            instruction=instruction,
            context_flags=context_flags,
        ):
            event_type = event.get("type", "message")
            yield format_sse(event, event=event_type)
    except Exception as exc:
        logger.exception("detailed_outline_generate_sse_error")
        yield sse_error(error=str(exc) or "细纲生成失败")
    yield sse_done()


@router.post("/projects/{project_id}/outlines/{outline_id}/detailed_outlines/generate")
def generate_all(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    outline_id: str,
    body: DetailedOutlineGenerateRequest,
    x_llm_api_key: str | None = Header(default=None, alias="X-LLM-API-Key", max_length=4096),
):
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)
    require_outline_viewer(db, outline_id=outline_id, user_id=user_id)

    context_flags: dict | None = None
    if body.context is not None:
        context_flags = body.context.model_dump()

    return create_sse_response(
        _generate_all_sse_events(
            outline_id=outline_id,
            project_id=project_id,
            user_id=user_id,
            request_id=request_id,
            db=db,
            x_llm_api_key=x_llm_api_key,
            chapters_per_volume=body.chapters_per_volume,
            instruction=body.instruction,
            context_flags=context_flags,
        )
    )


def _generate_single_volume_sse_events(
    outline_id: str,
    project_id: str,
    user_id: str,
    request_id: str,
    volume_number: int,
    db: DbDep,
    *,
    x_llm_api_key: str | None = None,
    chapters_per_volume: int | None = None,
    instruction: str | None = None,
    context_flags: dict | None = None,
) -> Iterator[str]:
    """Generate detailed outline for a single volume, yielding SSE events."""
    try:
        outline = db.get(Outline, outline_id)
        if outline is None:
            yield sse_error(error="Outline not found")
            yield sse_done()
            return
        project = db.get(Project, project_id)
        if project is None:
            yield sse_error(error="Project not found")
            yield sse_done()
            return

        # Extract volumes and find the target
        volumes = extract_volumes_from_outline(outline, db)
        target_vol = None
        for vol in volumes:
            if vol.number == volume_number:
                target_vol = vol
                break

        if target_vol is None:
            yield sse_error(error=f"Volume {volume_number} not found in outline")
            yield sse_done()
            return

        # Resolve LLM config
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
            yield sse_error(error="LLM 配置未找到，请先在 Prompts 页保存 LLM 配置")
            yield sse_done()
            return

        yield format_sse(
            {"type": "volume_start", "volume_number": target_vol.number, "volume_title": target_vol.title},
            event="volume_start",
        )
        try:
            result = generate_detailed_outline_for_volume(
                outline,
                target_vol,
                project,
                resolved.llm_call,
                str(resolved.api_key),
                request_id,
                user_id,
                db,
                chapters_per_volume=chapters_per_volume,
                instruction=instruction,
                context_flags=context_flags,
            )
            yield format_sse(
                {
                    "type": "volume_complete",
                    "volume_number": target_vol.number,
                    "chapter_count": result.chapter_count,
                    "detailed_outline_id": result.detailed_outline_id,
                },
                event="volume_complete",
            )
        except AppError as exc:
            logger.warning(
                "detailed_outline_single_volume_error volume=%d code=%s msg=%s",
                volume_number, exc.code, exc.message,
            )
            yield sse_error(error=exc.message)
        except Exception as exc:
            logger.exception("detailed_outline_single_volume_unexpected_error volume=%d", volume_number)
            yield sse_error(error=str(exc))
    except Exception as exc:
        logger.exception("detailed_outline_single_volume_sse_error")
        yield sse_error(error=str(exc) or "细纲生成失败")

    yield sse_done()


@router.post("/projects/{project_id}/outlines/{outline_id}/detailed_outlines/{volume_number}/generate")
def generate_single_volume(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    outline_id: str,
    volume_number: int,
    body: DetailedOutlineGenerateRequest,
    x_llm_api_key: str | None = Header(default=None, alias="X-LLM-API-Key", max_length=4096),
):
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)
    require_outline_viewer(db, outline_id=outline_id, user_id=user_id)

    context_flags: dict | None = None
    if body.context is not None:
        context_flags = body.context.model_dump()

    return create_sse_response(
        _generate_single_volume_sse_events(
            outline_id=outline_id,
            project_id=project_id,
            user_id=user_id,
            request_id=request_id,
            volume_number=volume_number,
            db=db,
            x_llm_api_key=x_llm_api_key,
            chapters_per_volume=body.chapters_per_volume,
            instruction=body.instruction,
            context_flags=context_flags,
        )
    )


# ---------------------------------------------------------------------------
# Chapter creation from detailed outline
# ---------------------------------------------------------------------------

@router.post("/detailed_outlines/{detailed_outline_id}/create_chapters")
def create_chapters(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    detailed_outline_id: str,
    replace: bool = Query(default=False),
    x_llm_api_key: str | None = Header(default=None, alias="X-LLM-API-Key", max_length=4096),
) -> dict:
    request_id = request.state.request_id
    _require_detailed_outline_editor(db, detailed_outline_id=detailed_outline_id, user_id=user_id)

    detail = db.get(DetailedOutline, detailed_outline_id)
    if detail is None:
        raise AppError.not_found("DetailedOutline not found")

    needs_generation = True
    if detail.structure_json:
        try:
            structure = json.loads(detail.structure_json)
            if isinstance(structure, dict):
                chapters = structure.get("chapters")
                if isinstance(chapters, list) and len(chapters) > 0:
                    needs_generation = False
        except Exception:
            pass

    if needs_generation:
        outline = db.get(Outline, detail.outline_id)
        project = db.get(Project, detail.project_id)
        if outline is None or project is None:
            raise AppError.not_found("Outline or Project not found")

        from app.services.detailed_outline_generation.models import VolumeInfo

        vol_info = VolumeInfo(
            number=detail.volume_number,
            title=detail.volume_title or "",
            beats_text=detail.content_md or "",
            chapter_range_start=1,
            chapter_range_end=0,
        )

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
            raise AppError(
                code="LLM_CONFIG_NOT_FOUND",
                message="LLM 配置未找到，请先在 Prompts 页保存 LLM 配置",
                status_code=400,
            )

        generate_detailed_outline_for_volume(
            outline,
            vol_info,
            project,
            resolved.llm_call,
            str(resolved.api_key),
            request_id,
            user_id,
            db,
        )
        db.refresh(detail)

    chapters = create_chapters_from_detailed_outline(
        detailed_outline_id,
        db,
        replace=replace,
    )
    return ok_payload(
        request_id=request_id,
        data={"chapters": chapters, "count": len(chapters)},
    )


# ---------------------------------------------------------------------------
# Chapter skeleton streaming generation (SSE)
# ---------------------------------------------------------------------------

def _generate_chapter_skeleton_sse_events(
    detailed_outline: DetailedOutline,
    outline: Outline,
    project: Project,
    user_id: str,
    request_id: str,
    db: DbDep,
    *,
    llm_call,
    api_key: str,
    neighbor_summaries: dict | None = None,
    chapters_count: int | None = None,
    instruction: str | None = None,
    context_flags: dict | None = None,
    replace_chapters: bool = True,
) -> Iterator[str]:
    """Wrap generate_chapter_skeleton_stream_events into SSE strings."""
    try:
        yield from generate_chapter_skeleton_stream_events(
            request_id=request_id,
            detailed_outline=detailed_outline,
            outline=outline,
            project=project,
            llm_call=llm_call,
            api_key=api_key,
            user_id=user_id,
            db=db,
            neighbor_summaries=neighbor_summaries,
            chapters_count=chapters_count,
            instruction=instruction,
            context_flags=context_flags,
            replace_chapters=replace_chapters,
        )
    except Exception as exc:
        logger.exception("chapter_skeleton_generate_sse_error")
        yield sse_error(error=str(exc) or "章节骨架生成失败")
        yield sse_done()


@router.post("/detailed_outlines/{detailed_outline_id}/generate_chapters_stream")
def generate_chapters_stream(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    detailed_outline_id: str,
    body: ChapterSkeletonGenerateRequest,
    x_llm_api_key: str | None = Header(default=None, alias="X-LLM-API-Key", max_length=4096),
):
    """Generate chapter skeleton for a detailed outline volume via SSE streaming."""
    request_id = request.state.request_id
    detail = _require_detailed_outline_editor(db, detailed_outline_id=detailed_outline_id, user_id=user_id)

    outline = db.get(Outline, detail.outline_id)
    project = db.get(Project, detail.project_id)
    if outline is None or project is None:
        raise AppError.not_found("Outline or Project not found")

    # Resolve LLM config
    resolved = None
    for task_key in ("chapter_skeleton_generate", "detailed_outline_generate", "outline_generate"):
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
        raise AppError(
            code="LLM_CONFIG_NOT_FOUND",
            message="LLM 配置未找到，请先在 Prompts 页保存 LLM 配置",
            status_code=400,
        )

    # Collect neighbor volume summaries for context
    neighbor_summaries = _collect_neighbor_summaries(db, detail)

    context_flags: dict | None = None
    if body.context is not None:
        context_flags = body.context.model_dump()

    return create_sse_response(
        _generate_chapter_skeleton_sse_events(
            detailed_outline=detail,
            outline=outline,
            project=project,
            user_id=user_id,
            request_id=request_id,
            db=db,
            llm_call=resolved.llm_call,
            api_key=str(resolved.api_key),
            neighbor_summaries=neighbor_summaries,
            chapters_count=body.chapters_count,
            instruction=body.instruction,
            context_flags=context_flags,
            replace_chapters=body.replace_chapters,
        )
    )


def _collect_neighbor_summaries(db: DbDep, detail: DetailedOutline) -> dict[str, str]:
    """Collect summaries from neighboring volumes for context continuity."""
    result: dict[str, str] = {}

    # Previous volume
    prev = db.execute(
        select(DetailedOutline).where(
            DetailedOutline.outline_id == detail.outline_id,
            DetailedOutline.volume_number == detail.volume_number - 1,
        )
    ).scalar_one_or_none()
    if prev is not None:
        result["previous"] = (prev.content_md or "")[:500]

    # Next volume
    nxt = db.execute(
        select(DetailedOutline).where(
            DetailedOutline.outline_id == detail.outline_id,
            DetailedOutline.volume_number == detail.volume_number + 1,
        )
    ).scalar_one_or_none()
    if nxt is not None:
        result["next"] = (nxt.content_md or "")[:500]

    return result
