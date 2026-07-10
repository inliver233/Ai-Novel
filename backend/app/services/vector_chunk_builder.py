from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.chapter import Chapter
from app.models.outline import Outline
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


def build_project_chunks(*, db: Session, project_id: str, sources: list[VectorSource] | None = None) -> list[VectorChunk]:
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
            db.execute(select(StoryMemory).where(StoryMemory.project_id == project_id).order_by(StoryMemory.updated_at.desc()))
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
