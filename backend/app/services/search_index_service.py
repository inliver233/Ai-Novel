from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import exception_log_fields, log_event
from app.db.session import SessionLocal
from app.db.utils import new_id, utc_now
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.project_task import ProjectTask
from app.models.outline import Outline
from app.models.project_source_document import ProjectSourceDocument
from app.models.search_index import SearchDocument
from app.models.story_memory import StoryMemory
from app.models.entry import Entry
from app.services.project_task_event_service import emit_and_enqueue_project_task, reset_project_task_to_queued

logger = logging.getLogger("ainovel")

_MAX_TITLE_CHARS = 400
_MAX_CONTENT_CHARS = 6000
_MAX_QUERY_TERMS = 8
_MAX_SEARCH_OFFSET = 10_000

_SAFE_FTS_TERM_RE = re.compile(r"^[0-9A-Za-z_]+$")


@dataclass(frozen=True, slots=True)
class SearchDocInput:
    source_type: str
    source_id: str
    title: str
    content: str
    url_path: str | None = None
    locator_json: str | None = None


def _trim(s: str | None) -> str:
    return (s or "").strip()


def _truncate(s: str, *, limit: int) -> str:
    text = (s or "").strip()
    if not text:
        return ""
    if limit <= 0:
        return text
    return text[:limit]


def _has_table(db: Session, *, name: str) -> bool:
    try:
        return bool(inspect(db.get_bind()).has_table(name))
    except Exception:
        return False


def _sqlite_table_exists(db: Session, *, name: str) -> bool:
    try:
        dialect = str(getattr(db.get_bind().dialect, "name", "") or "")
    except Exception:
        dialect = ""
    if dialect != "sqlite":
        return False
    try:
        row = db.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name LIMIT 1"), {"name": name}).first()
        return row is not None
    except Exception:
        return False


def _fts_enabled(db: Session) -> bool:
    return _sqlite_table_exists(db, name="search_index")


def _fts_delete(db: Session, *, rowid: int, title: str, content: str) -> None:
    # External content sync: delete requires the values currently stored in the index.
    db.execute(
        text("INSERT INTO search_index(search_index,rowid,title,content) VALUES('delete',:rowid,:title,:content)"),
        {"rowid": int(rowid), "title": title, "content": content},
    )


def _fts_upsert(db: Session, *, rowid: int, title: str, content: str) -> None:
    db.execute(
        text("INSERT INTO search_index(rowid,title,content) VALUES(:rowid,:title,:content)"),
        {"rowid": int(rowid), "title": title, "content": content},
    )


def upsert_search_document(
    *,
    db: Session,
    project_id: str,
    source_type: str,
    source_id: str,
    title: str,
    content: str,
    url_path: str | None = None,
    locator_json: str | None = None,
) -> SearchDocument:
    st = str(source_type or "").strip()
    sid = str(source_id or "").strip()
    pid = str(project_id or "").strip()
    if not (pid and st and sid):
        raise ValueError("project_id/source_type/source_id are required")

    title_norm = _truncate(_trim(title), limit=_MAX_TITLE_CHARS) or ""
    content_norm = _truncate(_trim(content), limit=_MAX_CONTENT_CHARS) or ""

    row = (
        db.execute(
            select(SearchDocument).where(
                SearchDocument.project_id == pid,
                SearchDocument.source_type == st,
                SearchDocument.source_id == sid,
            )
        )
        .scalars()
        .first()
    )
    fts = _fts_enabled(db)
    if row is None:
        row = SearchDocument(
            project_id=pid,
            source_type=st,
            source_id=sid,
            title=title_norm or None,
            content=content_norm,
            url_path=str(url_path or "").strip() or None,
            locator_json=str(locator_json or "").strip() or None,
            deleted_at=None,
        )
        db.add(row)
        db.flush()
        if fts:
            _fts_upsert(db, rowid=int(row.id), title=title_norm, content=content_norm)
        return row

    old_title = _trim(row.title)
    old_content = _trim(row.content)
    row.title = title_norm or None
    row.content = content_norm
    row.url_path = str(url_path or "").strip() or None
    row.locator_json = str(locator_json or "").strip() or None
    row.deleted_at = None
    db.flush()

    if fts:
        _fts_delete(db, rowid=int(row.id), title=old_title, content=old_content)
        _fts_upsert(db, rowid=int(row.id), title=title_norm, content=content_norm)
    return row


def delete_search_document(*, db: Session, project_id: str, source_type: str, source_id: str) -> bool:
    st = str(source_type or "").strip()
    sid = str(source_id or "").strip()
    pid = str(project_id or "").strip()
    if not (pid and st and sid):
        return False

    row = (
        db.execute(
            select(SearchDocument).where(
                SearchDocument.project_id == pid,
                SearchDocument.source_type == st,
                SearchDocument.source_id == sid,
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        return False

    if _fts_enabled(db):
        _fts_delete(db, rowid=int(row.id), title=_trim(row.title), content=_trim(row.content))
    db.delete(row)
    return True


def build_project_search_docs(*, db: Session, project_id: str) -> list[SearchDocInput]:
    pid = str(project_id or "").strip()
    if not pid:
        return []

    out: list[SearchDocInput] = []

    chapters = (
        db.execute(select(Chapter).where(Chapter.project_id == pid).order_by(Chapter.updated_at.desc()))
        .scalars()
        .all()
    )
    for c in chapters:
        title = _trim(c.title)
        header = f"第 {int(c.number)} 章：{title}".strip("：")
        plan = _trim(getattr(c, "plan", None))
        summary = _trim(getattr(c, "summary", None))
        content_md = _trim(getattr(c, "content_md", None))
        content = "\n\n".join([x for x in [header, plan, summary, content_md] if x]).strip()
        if not (summary or content_md):
            continue
        out.append(
            SearchDocInput(
                source_type="chapter",
                source_id=str(c.id),
                title=header,
                content=content,
                url_path=f"/projects/{pid}/writing?chapterId={str(c.id)}",
                locator_json=json.dumps({"chapter_id": str(c.id)}, ensure_ascii=False),
            )
        )

    characters = (
        db.execute(select(Character).where(Character.project_id == pid).order_by(Character.updated_at.desc()))
        .scalars()
        .all()
    )
    for ch in characters:
        name = _trim(ch.name)
        role = _trim(ch.role)
        profile = _trim(ch.profile)
        notes = _trim(ch.notes)
        body = "\n\n".join([x for x in [role, profile, notes] if x])
        out.append(
            SearchDocInput(
                source_type="character",
                source_id=str(ch.id),
                title=name or "角色卡",
                content=(name + "\n\n" + body).strip(),
                url_path=f"/projects/{pid}/characters",
                locator_json=json.dumps({"character_id": str(ch.id)}, ensure_ascii=False),
            )
        )

    story_memories = (
        db.execute(select(StoryMemory).where(StoryMemory.project_id == pid).order_by(StoryMemory.updated_at.desc()))
        .scalars()
        .all()
    )
    for m in story_memories:
        mt = _trim(getattr(m, "memory_type", "story_memory"))
        title = _trim(m.title) or mt
        content = _trim(m.content)
        full_context = _trim(getattr(m, "full_context_md", None))
        if not (content or full_context):
            continue
        out.append(
            SearchDocInput(
                source_type="story_memory",
                source_id=str(m.id),
                title=title,
                content="\n\n".join([x for x in [title, content, full_context] if x]).strip(),
                url_path=None,
                locator_json=json.dumps(
                    {"story_memory_id": str(m.id), "chapter_id": str(getattr(m, "chapter_id", "") or "").strip() or None},
                    ensure_ascii=False,
                ),
            )
        )

    outlines = (
        db.execute(select(Outline).where(Outline.project_id == pid).order_by(Outline.updated_at.desc()))
        .scalars()
        .all()
    )
    for o in outlines:
        title = _trim(o.title)
        content = _trim(o.content_md)
        if not (title or content):
            continue
        out.append(
            SearchDocInput(
                source_type="outline",
                source_id=str(o.id),
                title=title or "大纲",
                content=(title + "\n\n" + content).strip(),
                url_path=f"/projects/{pid}/outline",
                locator_json=json.dumps({"outline_id": str(o.id)}, ensure_ascii=False),
            )
        )

    if _has_table(db, name="project_source_documents"):
        source_docs = (
            db.execute(
                select(ProjectSourceDocument)
                .where(ProjectSourceDocument.project_id == pid)
                .order_by(ProjectSourceDocument.updated_at.desc())
            )
            .scalars()
            .all()
        )
        for d in source_docs:
            filename = _trim(getattr(d, "filename", ""))
            content = _trim(getattr(d, "content_text", ""))
            content_type = _trim(getattr(d, "content_type", ""))
            if not (filename or content):
                continue
            title = filename or "导入文档"
            body = "\n\n".join([x for x in [filename, content_type, content] if x]).strip()
            out.append(
                SearchDocInput(
                    source_type="source_document",
                    source_id=str(d.id),
                    title=title,
                    content=body,
                    url_path=f"/projects/{pid}/import?docId={str(d.id)}",
                    locator_json=json.dumps({"document_id": str(d.id)}, ensure_ascii=False),
                )
            )

    return out


def rebuild_project_search_index(*, db: Session, project_id: str) -> dict[str, Any]:
    """
    Full rebuild at project scope:
    - Compute the desired doc set for the project.
    - Upsert each document.
    - Delete stale docs that are no longer present.
    """

    pid = str(project_id or "").strip()
    if not pid:
        return {"ok": False, "reason": "project_id_empty", "upserted": 0, "deleted": 0}

    docs = build_project_search_docs(db=db, project_id=pid)
    desired = {(d.source_type, d.source_id) for d in docs}

    upserted = 0
    for d in docs:
        upsert_search_document(
            db=db,
            project_id=pid,
            source_type=d.source_type,
            source_id=d.source_id,
            title=d.title,
            content=d.content,
            url_path=d.url_path,
            locator_json=d.locator_json,
        )
        upserted += 1

    deleted = 0
    existing = (
        db.execute(select(SearchDocument).where(SearchDocument.project_id == pid))
        .scalars()
        .all()
    )
    for row in existing:
        key = (str(row.source_type), str(row.source_id))
        if key in desired:
            continue
        if delete_search_document(db=db, project_id=pid, source_type=str(row.source_type), source_id=str(row.source_id)):
            deleted += 1

    return {"ok": True, "project_id": pid, "upserted": int(upserted), "deleted": int(deleted), "fts_enabled": _fts_enabled(db)}


def rebuild_project_search_index_async(*, project_id: str) -> dict[str, Any]:
    """
    Session-owning helper that avoids holding a long transaction while rendering source docs.
    """

    pid = str(project_id or "").strip()
    if not pid:
        return {"ok": False, "reason": "project_id_empty"}

    db_read = SessionLocal()
    try:
        docs = build_project_search_docs(db=db_read, project_id=pid)
    finally:
        db_read.close()

    db_write = SessionLocal()
    try:
        desired = {(d.source_type, d.source_id) for d in docs}

        upserted = 0
        for d in docs:
            upsert_search_document(
                db=db_write,
                project_id=pid,
                source_type=d.source_type,
                source_id=d.source_id,
                title=d.title,
                content=d.content,
                url_path=d.url_path,
                locator_json=d.locator_json,
            )
            upserted += 1

        deleted = 0
        existing = db_write.execute(select(SearchDocument).where(SearchDocument.project_id == pid)).scalars().all()
        for row in existing:
            key = (str(row.source_type), str(row.source_id))
            if key in desired:
                continue
            if delete_search_document(db=db_write, project_id=pid, source_type=str(row.source_type), source_id=str(row.source_id)):
                deleted += 1

        db_write.commit()
        return {"ok": True, "project_id": pid, "upserted": int(upserted), "deleted": int(deleted), "fts_enabled": _fts_enabled(db_write)}
    except Exception as exc:
        db_write.rollback()
        log_event(
            logger,
            "warning",
            event="SEARCH_INDEX_REBUILD_ERROR",
            project_id=pid,
            error_type=type(exc).__name__,
            **exception_log_fields(exc),
        )
        return {"ok": False, "project_id": pid, "error_type": type(exc).__name__}
    finally:
        db_write.close()


def _fts_query_literal(q: str) -> str:
    s = (q or "").strip()
    if not s:
        return ""
    s = s.replace('"', '""')
    return f"\"{s}\""


def _split_query_terms(q: str) -> list[str]:
    q_norm = (q or "").strip()
    if not q_norm:
        return []
    parts = [p.strip() for p in re.split(r"\s+", q_norm) if p.strip()]
    return parts[:_MAX_QUERY_TERMS]


def _fts_query_fuzzy(q: str) -> str:
    parts = _split_query_terms(q)
    if not parts:
        return ""

    def render_term(t: str) -> str:
        t_norm = (t or "").strip()
        if not t_norm:
            return ""
        if _SAFE_FTS_TERM_RE.match(t_norm) and len(t_norm) >= 2:
            return f"{t_norm}*"
        return _fts_query_literal(t_norm)

    rendered = [render_term(p) for p in parts]
    rendered = [x for x in rendered if x]
    return " ".join(rendered).strip()


def _like_snippet(*, content: str, q: str, window: int = 120) -> str:
    text_s = (content or "").strip()
    q_s = (q or "").strip()
    if not text_s:
        return ""
    if not q_s:
        return _truncate(text_s, limit=window * 2)
    idx = text_s.lower().find(q_s.lower())
    if idx < 0:
        return _truncate(text_s, limit=window * 2)
    start = max(0, idx - window)
    end = min(len(text_s), idx + len(q_s) + window)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text_s) else ""
    return f"{prefix}{text_s[start:end]}{suffix}"


def query_project_search(
    *,
    db: Session,
    project_id: str,
    q: str,
    sources: list[str] | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    # Keep the direct path usable even when search_documents was never rebuilt. Every
    # source query is bounded so matching ORM rows are not loaded into Python without limit.

    pid = str(project_id or "").strip()
    q_raw = str(q or "").strip()
    sources_norm = [str(s or "").strip() for s in (sources or []) if str(s or "").strip()]
    limit = max(1, min(int(limit or 20), 200))
    offset = max(0, min(int(offset or 0), _MAX_SEARCH_OFFSET))

    if not pid:
        return {"items": [], "next_offset": None, "mode": "none", "fts_enabled": False}
    if not q_raw:
        return {"items": [], "next_offset": None, "mode": "empty", "fts_enabled": False}

    terms = _split_query_terms(q_raw)
    if not terms:
        return {"items": [], "next_offset": None, "mode": "empty", "fts_enabled": False}

    q_primary = terms[0]
    terms_lower = [t.lower() for t in terms if t]
    if not terms_lower:
        return {"items": [], "next_offset": None, "mode": "empty", "fts_enabled": False}

    all_types = ["chapter", "outline", "character", "story_memory", "source_document", "entry"]
    if sources_norm:
        search_types = [s for s in sources_norm if s in all_types]
        if not search_types:
            return {"items": [], "next_offset": None, "mode": "direct", "fts_enabled": False}
    else:
        search_types = all_types

    # Local import to keep the change scoped to this function.
    from sqlalchemy import String, case, cast, func, literal  # type: ignore

    def _escape_like(term: str) -> str:
        # Escape LIKE wildcards to keep search behaviour closer to "contains" semantics.
        return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def _like_all_terms(expr) -> list[Any]:
        expr_l = func.lower(expr)
        conds: list[Any] = []
        for t in terms_lower:
            conds.append(expr_l.like(f"%{_escape_like(t)}%", escape="\\"))
        return conds

    candidate_limit = offset + limit + 1
    primary_like_pattern = f"%{_escape_like(q_primary.lower())}%"

    def _bounded_candidates(stmt: Any, *, title_expr: Any, updated_at: Any, source_id: Any) -> Any:
        title_hit_rank = case(
            (func.lower(title_expr).like(primary_like_pattern, escape="\\"), 0),
            else_=1,
        )
        return stmt.order_by(title_hit_rank.asc(), updated_at.desc(), source_id.asc()).limit(candidate_limit)

    ranked: list[dict[str, Any]] = []

    if "chapter" in search_types:
        chapter_title_expr = (
            literal("第 ")
            + cast(Chapter.number, String)
            + literal(" 章：")
            + func.coalesce(Chapter.title, "")
        )
        chapter_expr = (
            func.coalesce(Chapter.title, "")
            + "\n\n"
            + func.coalesce(Chapter.plan, "")
            + "\n\n"
            + func.coalesce(Chapter.summary, "")
            + "\n\n"
            + func.coalesce(Chapter.content_md, "")
        )
        chapters = (
            db.execute(
                _bounded_candidates(
                    select(Chapter).where(Chapter.project_id == pid, *_like_all_terms(chapter_expr)),
                    title_expr=chapter_title_expr,
                    updated_at=Chapter.updated_at,
                    source_id=Chapter.id,
                )
            )
            .scalars()
            .all()
        )
        for c in chapters:
            title = _trim(getattr(c, "title", None))
            header = f"第 {int(getattr(c, 'number', 0) or 0)} 章：{title}".strip("：")
            plan = _trim(getattr(c, "plan", None))
            summary = _trim(getattr(c, "summary", None))
            content_md = _trim(getattr(c, "content_md", None))
            content = "\n\n".join([x for x in [header, plan, summary, content_md] if x]).strip()
            if not content:
                continue
            updated_at = getattr(c, "updated_at", None)
            updated_ts = float(updated_at.timestamp()) if updated_at is not None else 0.0
            ranked.append(
                {
                    "title_hit": 0 if q_primary.lower() in header.lower() else 1,
                    "updated_ts": updated_ts,
                    "item": {
                        "source_type": "chapter",
                        "source_id": str(getattr(c, "id", "") or ""),
                        "title": header,
                        "snippet": _like_snippet(content=content, q=q_primary),
                        "jump_url": f"/projects/{pid}/writing?chapterId={str(getattr(c, 'id', '') or '')}",
                        "locator_json": json.dumps({"chapter_id": str(getattr(c, "id", "") or "")}, ensure_ascii=False),
                    },
                }
            )

    if "character" in search_types:
        character_expr = (
            func.coalesce(Character.name, "")
            + "\n\n"
            + func.coalesce(Character.role, "")
            + "\n\n"
            + func.coalesce(Character.profile, "")
            + "\n\n"
            + func.coalesce(Character.notes, "")
        )
        characters = (
            db.execute(
                _bounded_candidates(
                    select(Character).where(Character.project_id == pid, *_like_all_terms(character_expr)),
                    title_expr=func.coalesce(Character.name, ""),
                    updated_at=Character.updated_at,
                    source_id=Character.id,
                )
            )
            .scalars()
            .all()
        )
        for ch in characters:
            name = _trim(getattr(ch, "name", None))
            role = _trim(getattr(ch, "role", None))
            profile = _trim(getattr(ch, "profile", None))
            notes = _trim(getattr(ch, "notes", None))
            body = "\n\n".join([x for x in [role, profile, notes] if x])
            content = (name + "\n\n" + body).strip()
            if not content:
                continue
            updated_at = getattr(ch, "updated_at", None)
            updated_ts = float(updated_at.timestamp()) if updated_at is not None else 0.0
            ranked.append(
                {
                    "title_hit": 0 if q_primary.lower() in name.lower() else 1,
                    "updated_ts": updated_ts,
                    "item": {
                        "source_type": "character",
                        "source_id": str(getattr(ch, "id", "") or ""),
                        "title": name or "角色卡",
                        "snippet": _like_snippet(content=content, q=q_primary),
                        "jump_url": f"/projects/{pid}/characters",
                        "locator_json": json.dumps({"character_id": str(getattr(ch, "id", "") or "")}, ensure_ascii=False),
                    },
                }
            )

    if "story_memory" in search_types:
        memory_expr = (
            func.coalesce(StoryMemory.memory_type, "")
            + "\n\n"
            + func.coalesce(StoryMemory.title, "")
            + "\n\n"
            + func.coalesce(StoryMemory.content, "")
            + "\n\n"
            + func.coalesce(StoryMemory.full_context_md, "")
        )
        memories = (
            db.execute(
                _bounded_candidates(
                    select(StoryMemory).where(StoryMemory.project_id == pid, *_like_all_terms(memory_expr)),
                    title_expr=func.coalesce(
                        func.nullif(func.trim(StoryMemory.title), ""),
                        func.nullif(func.trim(StoryMemory.memory_type), ""),
                        "story_memory",
                    ),
                    updated_at=StoryMemory.updated_at,
                    source_id=StoryMemory.id,
                )
            )
            .scalars()
            .all()
        )
        for m in memories:
            mt = _trim(getattr(m, "memory_type", None)) or "story_memory"
            title = _trim(getattr(m, "title", None)) or mt
            content_body = _trim(getattr(m, "content", None))
            full_context = _trim(getattr(m, "full_context_md", None))
            content = "\n\n".join([x for x in [title, content_body, full_context] if x]).strip()
            if not content:
                continue
            updated_at = getattr(m, "updated_at", None)
            updated_ts = float(updated_at.timestamp()) if updated_at is not None else 0.0
            ranked.append(
                {
                    "title_hit": 0 if q_primary.lower() in title.lower() else 1,
                    "updated_ts": updated_ts,
                    "item": {
                        "source_type": "story_memory",
                        "source_id": str(getattr(m, "id", "") or ""),
                        "title": title,
                        "snippet": _like_snippet(content=content, q=q_primary),
                        "jump_url": None,
                        "locator_json": json.dumps(
                            {
                                "story_memory_id": str(getattr(m, "id", "") or ""),
                                "chapter_id": str(getattr(m, "chapter_id", "") or "").strip() or None,
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            )

    if "outline" in search_types:
        outline_expr = func.coalesce(Outline.title, "") + "\n\n" + func.coalesce(Outline.content_md, "")
        outlines = (
            db.execute(
                _bounded_candidates(
                    select(Outline).where(Outline.project_id == pid, *_like_all_terms(outline_expr)),
                    title_expr=func.coalesce(func.nullif(func.trim(Outline.title), ""), "大纲"),
                    updated_at=Outline.updated_at,
                    source_id=Outline.id,
                )
            )
            .scalars()
            .all()
        )
        for o in outlines:
            title = _trim(getattr(o, "title", None)) or "大纲"
            content_md = _trim(getattr(o, "content_md", None))
            content = (title + "\n\n" + content_md).strip()
            if not content:
                continue
            updated_at = getattr(o, "updated_at", None)
            updated_ts = float(updated_at.timestamp()) if updated_at is not None else 0.0
            ranked.append(
                {
                    "title_hit": 0 if q_primary.lower() in title.lower() else 1,
                    "updated_ts": updated_ts,
                    "item": {
                        "source_type": "outline",
                        "source_id": str(getattr(o, "id", "") or ""),
                        "title": title,
                        "snippet": _like_snippet(content=content, q=q_primary),
                        "jump_url": f"/projects/{pid}/outline",
                        "locator_json": json.dumps({"outline_id": str(getattr(o, "id", "") or "")}, ensure_ascii=False),
                    },
                }
            )

    if "source_document" in search_types and _has_table(db, name="project_source_documents"):
        doc_expr = (
            func.coalesce(ProjectSourceDocument.filename, "")
            + "\n\n"
            + func.coalesce(ProjectSourceDocument.content_type, "")
            + "\n\n"
            + func.coalesce(ProjectSourceDocument.content_text, "")
        )
        docs = (
            db.execute(
                _bounded_candidates(
                    select(ProjectSourceDocument).where(
                        ProjectSourceDocument.project_id == pid,
                        *_like_all_terms(doc_expr),
                    ),
                    title_expr=func.coalesce(
                        func.nullif(func.trim(ProjectSourceDocument.filename), ""),
                        "导入文档",
                    ),
                    updated_at=ProjectSourceDocument.updated_at,
                    source_id=ProjectSourceDocument.id,
                )
            )
            .scalars()
            .all()
        )
        for d in docs:
            filename = _trim(getattr(d, "filename", None))
            content_type = _trim(getattr(d, "content_type", None))
            content_text = _trim(getattr(d, "content_text", None))
            title = filename or "导入文档"
            content = "\n\n".join([x for x in [filename, content_type, content_text] if x]).strip()
            if not content:
                continue
            updated_at = getattr(d, "updated_at", None)
            updated_ts = float(updated_at.timestamp()) if updated_at is not None else 0.0
            ranked.append(
                {
                    "title_hit": 0 if q_primary.lower() in title.lower() else 1,
                    "updated_ts": updated_ts,
                    "item": {
                        "source_type": "source_document",
                        "source_id": str(getattr(d, "id", "") or ""),
                        "title": title,
                        "snippet": _like_snippet(content=content, q=q_primary),
                        "jump_url": f"/projects/{pid}/import?docId={str(getattr(d, 'id', '') or '')}",
                        "locator_json": json.dumps({"document_id": str(getattr(d, "id", "") or "")}, ensure_ascii=False),
                    },
                }
            )

    if "entry" in search_types:
        entry_expr = (
            func.coalesce(Entry.title, "")
            + "\n\n"
            + func.coalesce(Entry.content, "")
        )
        entries = (
            db.execute(
                _bounded_candidates(
                    select(Entry).where(Entry.project_id == pid, *_like_all_terms(entry_expr)),
                    title_expr=func.coalesce(func.nullif(func.trim(Entry.title), ""), "条目"),
                    updated_at=Entry.updated_at,
                    source_id=Entry.id,
                )
            )
            .scalars()
            .all()
        )
        for e in entries:
            title = _trim(getattr(e, "title", None)) or "条目"
            content = _trim(getattr(e, "content", None))
            full_content = (title + "\n\n" + content).strip()
            if not full_content:
                continue
            updated_at = getattr(e, "updated_at", None)
            updated_ts = float(updated_at.timestamp()) if updated_at is not None else 0.0
            ranked.append(
                {
                    "title_hit": 0 if q_primary.lower() in title.lower() else 1,
                    "updated_ts": updated_ts,
                    "item": {
                        "source_type": "entry",
                        "source_id": str(getattr(e, "id", "") or ""),
                        "title": title,
                        "snippet": _like_snippet(content=full_content, q=q_primary),
                        "jump_url": f"/projects/{pid}/entries",
                        "locator_json": json.dumps({"entry_id": str(getattr(e, "id", "") or "")}, ensure_ascii=False),
                    },
                }
            )

    ranked.sort(
        key=lambda r: (
            int(r.get("title_hit", 1)),
            -float(r.get("updated_ts", 0.0)),
            str(getattr(getattr(r, "item", None), "source_type", "") or r.get("item", {}).get("source_type", "")),
            str(getattr(getattr(r, "item", None), "source_id", "") or r.get("item", {}).get("source_id", "")),
        )
    )

    total = len(ranked)
    page = ranked[offset : offset + limit]
    items_out = [r.get("item") for r in page if isinstance(r.get("item"), dict)]
    next_offset = (offset + limit) if (offset + limit) < total else None

    return {"items": items_out, "next_offset": next_offset, "mode": "direct", "fts_enabled": False}


def schedule_search_rebuild_task(
    *,
    db: Session | None = None,
    project_id: str,
    actor_user_id: str | None,
    request_id: str | None,
    reason: str,
) -> str | None:
    """
    Fail-soft scheduler: ensure/enqueue a ProjectTask(kind=search_rebuild) for the project.

    Idempotency key is derived from the latest succeeded search_rebuild task, so a new task can be created after each
    successful rebuild while still deduping bursts of changes.
    """

    pid = str(project_id or "").strip()
    if not pid:
        return None

    reason_norm = str(reason or "").strip() or "dirty"
    owns_session = db is None
    if db is None:
        db = SessionLocal()
    try:
        running = (
            db.execute(
                select(ProjectTask)
                .where(
                    ProjectTask.project_id == pid,
                    ProjectTask.kind == "search_rebuild",
                    ProjectTask.status == "running",
                )
                .order_by(ProjectTask.started_at.desc(), ProjectTask.created_at.desc(), ProjectTask.id.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )

        # If a rebuild is already running, changes may happen while it is executing (especially with async inline queue).
        # Schedule a follow-up rebuild so the index eventually converges to the latest project state.
        if running is not None:
            idempotency_key = f"search:project:after:{running.id}:v1"
            task = (
                db.execute(
                    select(ProjectTask).where(
                        ProjectTask.project_id == pid,
                        ProjectTask.idempotency_key == idempotency_key,
                    )
                )
                .scalars()
                .first()
            )

            created_task = False
            if task is None:
                created_task = True
                task = ProjectTask(
                    id=new_id(),
                    project_id=pid,
                    actor_user_id=actor_user_id,
                    kind="search_rebuild",
                    status="queued",
                    idempotency_key=idempotency_key,
                    params_json=json.dumps(
                        {
                            "reason": reason_norm,
                            "request_id": request_id,
                            "triggered_at": utc_now().isoformat().replace("+00:00", "Z"),
                            "after_task_id": str(running.id),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    result_json=None,
                    error_json=None,
                )
                db.add(task)
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    task = (
                        db.execute(
                            select(ProjectTask).where(
                                ProjectTask.project_id == pid,
                                ProjectTask.idempotency_key == idempotency_key,
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if task is None:
                        return None
            else:
                status_norm = str(getattr(task, "status", "") or "").strip().lower()
                event_type = None
                if status_norm not in {"queued", "running"}:
                    reset_project_task_to_queued(task=task, increment_retry_count=status_norm == "failed")
                    db.commit()
                    event_type = "retry" if status_norm == "failed" else "queued"
                else:
                    event_type = None

            return emit_and_enqueue_project_task(
                db,
                task=task,
                request_id=request_id,
                logger=logger,
                event_type=("queued" if created_task else event_type),
                source="scheduler",
                payload={"reason": reason_norm, "request_id": request_id, "after_task_id": str(running.id)},
            )

        last = (
            db.execute(
                select(ProjectTask)
                .where(
                    ProjectTask.project_id == pid,
                    ProjectTask.kind == "search_rebuild",
                    ProjectTask.status.in_(["succeeded", "done"]),
                )
                .order_by(ProjectTask.finished_at.desc(), ProjectTask.created_at.desc(), ProjectTask.id.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )

        token = "none"
        last_finished_at = getattr(last, "finished_at", None) if last is not None else None
        if last_finished_at is not None:
            token = last_finished_at.isoformat().replace("+00:00", "Z")

        idempotency_key = f"search:project:since:{token}:v1"
        task = (
            db.execute(
                select(ProjectTask).where(
                    ProjectTask.project_id == pid,
                    ProjectTask.idempotency_key == idempotency_key,
                )
            )
            .scalars()
            .first()
        )

        created_task = False
        if task is None:
            created_task = True
            task = ProjectTask(
                id=new_id(),
                project_id=pid,
                actor_user_id=actor_user_id,
                kind="search_rebuild",
                status="queued",
                idempotency_key=idempotency_key,
                params_json=json.dumps(
                    {"reason": reason_norm, "request_id": request_id, "triggered_at": utc_now().isoformat().replace("+00:00", "Z")},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                result_json=None,
                error_json=None,
            )
            db.add(task)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                task = (
                    db.execute(
                        select(ProjectTask).where(
                            ProjectTask.project_id == pid,
                            ProjectTask.idempotency_key == idempotency_key,
                        )
                    )
                    .scalars()
                    .first()
                )
                if task is None:
                    return None
        else:
            status_norm = str(getattr(task, "status", "") or "").strip().lower()
            event_type = None
            if status_norm not in {"queued", "running"}:
                reset_project_task_to_queued(task=task, increment_retry_count=status_norm == "failed")
                db.commit()
                event_type = "retry" if status_norm == "failed" else "queued"
            else:
                event_type = None
        return emit_and_enqueue_project_task(
            db,
            task=task,
            request_id=request_id,
            logger=logger,
            event_type=("queued" if created_task else event_type),
            source="scheduler",
            payload={"reason": reason_norm, "request_id": request_id},
        )
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        log_event(
            logger,
            "warning",
            event="SEARCH_REBUILD_SCHEDULE_ERROR",
            project_id=pid,
            error_type=type(exc).__name__,
            request_id=request_id,
            **exception_log_fields(exc),
        )
        return None
    finally:
        if owns_session:
            db.close()
