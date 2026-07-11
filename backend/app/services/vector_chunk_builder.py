from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.chapter import Chapter
from app.models.outline import Outline
from app.models.project_source_document import ProjectSourceDocument, ProjectSourceDocumentChunk
from app.models.story_memory import StoryMemory
from app.services.vector_types import VectorChunk, VectorSource, _ALL_SOURCES


def _chunk_text(text: str, *, chunk_size: int, overlap: int) -> list[str]:
    s = (text or "").strip()
    if not s:
        return []
    if chunk_size <= 0:
        return [s]

    out: list[str] = []
    start = 0
    overlap = max(0, min(int(overlap), int(chunk_size) - 1)) if chunk_size > 1 else 0
    while start < len(s):
        end = min(len(s), start + chunk_size)
        piece = s[start:end].strip()
        if piece:
            out.append(piece)
        if end >= len(s):
            break
        start = max(0, end - overlap)
    return out


def normalize_document_kb_id(kb_id: str | None) -> str:
    return str(kb_id or "").strip() or "default"


def build_source_document_vector_chunk(
    *,
    project_id: str,
    document_id: str,
    filename: str,
    content_type: str,
    kb_id: str | None,
    chunk_index: int,
    content_text: str,
    vector_chunk_id: str | None = None,
) -> VectorChunk:
    index = int(chunk_index)
    owner_kb_id = normalize_document_kb_id(kb_id)
    return VectorChunk(
        id=str(vector_chunk_id or "").strip() or f"source_doc:{document_id}:{index}",
        text=str(content_text or "").strip(),
        metadata={
            "project_id": project_id,
            # Keep the public three-source contract while making the true
            # origin explicit for ownership-safe rebuilds and cleanup.
            "source": "chapter",
            "source_origin": "project_source_document",
            "source_id": document_id,
            "source_document_id": document_id,
            "title": str(filename or "").strip(),
            "content_type": str(content_type or "txt").strip() or "txt",
            "knowledge_base_id": owner_kb_id,
            "chunk_index": index,
        },
    )


def build_kb_chunk_plan(
    *,
    db: Session,
    project_id: str,
    kb_ids: list[str],
    sources: list[VectorSource] | None = None,
) -> dict[str, list[VectorChunk]]:
    """Build authoritative, ownership-aware chunks for each requested KB.

    Project-native outline/chapter/story-memory content belongs only to the
    default KB. Imported documents belong only to their persisted ``kb_id``;
    legacy/null ownership is normalized to default. Persisted document chunks
    are authoritative, with deterministic content fallback for bundle imports
    that do not carry derived chunk rows.
    """

    selected: list[str] = []
    seen: set[str] = set()
    for raw in kb_ids:
        kb_id = normalize_document_kb_id(raw)
        if kb_id in seen:
            continue
        seen.add(kb_id)
        selected.append(kb_id)

    plan: dict[str, list[VectorChunk]] = {kb_id: [] for kb_id in selected}
    if not selected:
        return plan

    effective_sources = list(sources or _ALL_SOURCES)

    if "default" in plan:
        plan["default"].extend(build_project_chunks(db=db, project_id=project_id, sources=effective_sources))

    # Imported documents intentionally reuse the public ``chapter`` source
    # label, so the same source filter must govern them as native chapters.
    if "chapter" not in effective_sources:
        return plan

    documents = (
        db.execute(
            select(ProjectSourceDocument)
            .where(
                ProjectSourceDocument.project_id == project_id,
                ProjectSourceDocument.status == "done",
            )
            .order_by(ProjectSourceDocument.updated_at.desc(), ProjectSourceDocument.id.asc())
        )
        .scalars()
        .all()
    )
    owned_documents = [doc for doc in documents if normalize_document_kb_id(doc.kb_id) in plan]
    document_ids = [str(doc.id) for doc in owned_documents]
    persisted_by_document: dict[str, list[ProjectSourceDocumentChunk]] = {}
    if document_ids:
        persisted = (
            db.execute(
                select(ProjectSourceDocumentChunk)
                .where(ProjectSourceDocumentChunk.document_id.in_(document_ids))
                .order_by(
                    ProjectSourceDocumentChunk.document_id.asc(),
                    ProjectSourceDocumentChunk.chunk_index.asc(),
                )
            )
            .scalars()
            .all()
        )
        for row in persisted:
            persisted_by_document.setdefault(str(row.document_id), []).append(row)

    chunk_size = int(settings.vector_chunk_size or 800)
    overlap = int(settings.vector_chunk_overlap or 120)
    for doc in owned_documents:
        kb_id = normalize_document_kb_id(doc.kb_id)
        persisted_rows = persisted_by_document.get(str(doc.id), [])
        if persisted_rows:
            pieces = [
                (
                    int(row.chunk_index),
                    str(row.content_text or ""),
                    str(row.vector_chunk_id or "").strip() or None,
                )
                for row in persisted_rows
            ]
        else:
            pieces = [
                (index, content, None)
                for index, content in enumerate(
                    _chunk_text(str(doc.content_text or ""), chunk_size=chunk_size, overlap=overlap)
                )
            ]

        for index, content, vector_chunk_id in pieces:
            chunk = build_source_document_vector_chunk(
                project_id=project_id,
                document_id=str(doc.id),
                filename=str(doc.filename or ""),
                content_type=str(doc.content_type or "txt"),
                kb_id=kb_id,
                chunk_index=index,
                content_text=content,
                vector_chunk_id=vector_chunk_id,
            )
            if chunk.text:
                plan[kb_id].append(chunk)

    return plan


def build_project_chunks(
    *, db: Session, project_id: str, sources: list[VectorSource] | None = None
) -> list[VectorChunk]:
    sources = sources or list(_ALL_SOURCES)
    chunk_size = int(settings.vector_chunk_size or 800)
    overlap = int(settings.vector_chunk_overlap or 120)

    out: list[VectorChunk] = []

    if "outline" in sources:
        rows = (
            db.execute(select(Outline).where(Outline.project_id == project_id).order_by(Outline.updated_at.desc()))
            .scalars()
            .all()
        )
        for o in rows:
            title = (o.title or "").strip()
            content = (o.content_md or "").strip()
            text = f"{title}\n\n{content}".strip()
            for idx, chunk in enumerate(_chunk_text(text, chunk_size=chunk_size, overlap=overlap)):
                out.append(
                    VectorChunk(
                        id=f"outline:{o.id}:{idx}",
                        text=chunk,
                        metadata={
                            "project_id": project_id,
                            "source": "outline",
                            "source_id": o.id,
                            "title": title,
                            "chunk_index": idx,
                        },
                    )
                )

    if "chapter" in sources:
        rows = (
            db.execute(select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.updated_at.desc()))
            .scalars()
            .all()
        )
        for c in rows:
            title = (c.title or "").strip()
            content = (c.content_md or "").strip()
            if not content:
                continue
            header = f"第 {int(c.number)} 章：{title}".strip("：")
            text = f"{header}\n\n{content}".strip()
            for idx, chunk in enumerate(_chunk_text(text, chunk_size=chunk_size, overlap=overlap)):
                out.append(
                    VectorChunk(
                        id=f"chapter:{c.id}:{idx}",
                        text=chunk,
                        metadata={
                            "project_id": project_id,
                            "source": "chapter",
                            "source_id": c.id,
                            "chapter_number": int(c.number),
                            "title": title,
                            "chunk_index": idx,
                        },
                    )
                )

    if "story_memory" in sources:
        rows = (
            db.execute(
                select(StoryMemory).where(StoryMemory.project_id == project_id).order_by(StoryMemory.updated_at.desc())
            )
            .scalars()
            .all()
        )
        for m in rows:
            title = (m.title or "").strip()
            content = (m.content or "").strip()
            if not content:
                continue
            header = f"[{str(m.memory_type or '').strip() or 'story_memory'}] {title}".strip()
            text = f"{header}\n\n{content}".strip() if header else content
            for idx, chunk in enumerate(_chunk_text(text, chunk_size=chunk_size, overlap=overlap)):
                out.append(
                    VectorChunk(
                        id=f"story_memory:{m.id}:{idx}",
                        text=chunk,
                        metadata={
                            "project_id": project_id,
                            "source": "story_memory",
                            "source_id": m.id,
                            "title": title,
                            "chunk_index": idx,
                            "memory_type": str(m.memory_type or "").strip(),
                            "chapter_id": str(m.chapter_id or "") or None,
                            "story_timeline": int(m.story_timeline or 0),
                            "is_foreshadow": bool(int(m.is_foreshadow or 0)),
                        },
                    )
                )

    return out
