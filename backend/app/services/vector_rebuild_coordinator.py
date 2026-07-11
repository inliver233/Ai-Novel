from __future__ import annotations

from typing import Any, Literal

from sqlalchemy.orm import Session

from app.services.embedding_service import embed_texts as embed_texts_with_providers
from app.services.vector_chunk_builder import build_kb_chunk_plan, normalize_document_kb_id
from app.services.vector_storage import (
    _vector_enabled_reason,
    ingest_chunks_with_embeddings,
    rebuild_project_with_embeddings,
)
from app.services.vector_types import VectorChunk, VectorSource


VectorWriteOperation = Literal["ingest", "rebuild"]


def normalize_target_kb_ids(kb_ids: list[str]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for raw in kb_ids:
        kb_id = normalize_document_kb_id(raw)
        if kb_id in seen:
            continue
        seen.add(kb_id)
        selected.append(kb_id)
    return selected


def _write_kb_partition(
    *,
    operation: VectorWriteOperation,
    project_id: str,
    kb_id: str,
    chunks: list[VectorChunk],
    embeddings: list[list[float]],
) -> dict[str, Any]:
    # A custom KB is an authoritative document partition. Even the legacy
    # "ingest" endpoint must replace it so historical native-content pollution
    # and stale document chunks cannot survive. Default ingest remains an
    # additive/source-filter-friendly operation.
    if operation == "rebuild" or kb_id != "default":
        result = rebuild_project_with_embeddings(
            project_id=project_id,
            kb_id=kb_id,
            chunks=chunks,
            embeddings=embeddings,
        )
        if operation == "ingest":
            result = {**result, "ingested": int(result.get("rebuilt") or 0)}
        return result
    return ingest_chunks_with_embeddings(
        project_id=project_id,
        kb_id=kb_id,
        chunks=chunks,
        embeddings=embeddings,
    )


def _summarize_results(
    *,
    operation: VectorWriteOperation,
    selected: list[str],
    per_kb: dict[str, dict[str, Any]],
    unique_text_count: int,
) -> dict[str, Any]:
    ordered_per_kb = {kb_id: per_kb[kb_id] for kb_id in selected if kb_id in per_kb}
    results = list(ordered_per_kb.values())
    enabled = all(bool(result.get("enabled")) for result in results) if results else False
    skipped = all(bool(result.get("skipped")) for result in results) if results else True
    count_key = "rebuilt" if operation == "rebuild" else "ingested"
    total = sum(int(result.get(count_key) or 0) for result in results)
    return {
        "enabled": enabled,
        "skipped": skipped,
        "disabled_reason": next(
            (result.get("disabled_reason") for result in results if result.get("disabled_reason")), None
        ),
        count_key: total,
        "backend": next((result.get("backend") for result in results if result.get("backend")), None),
        "error": next((result.get("error") for result in results if result.get("error")), None),
        "embedding": {
            "calls": 1 if unique_text_count else 0,
            "unique_texts": unique_text_count,
        },
        "kbs": {"selected": selected, "per_kb": ordered_per_kb},
    }


def run_kb_vector_operation(
    *,
    db: Session,
    project_id: str,
    kb_ids: list[str],
    embedding: dict[str, str | None] | None,
    operation: VectorWriteOperation,
    sources: list[VectorSource] | None = None,
) -> dict[str, Any]:
    """Build, embed once, and partition writes across ownership-safe KBs."""

    selected = normalize_target_kb_ids(kb_ids)
    if not selected:
        return _summarize_results(
            operation=operation,
            selected=[],
            per_kb={},
            unique_text_count=0,
        )

    plan = build_kb_chunk_plan(db=db, project_id=project_id, kb_ids=selected, sources=sources)
    # All callers enter after committing authoritative content. End the
    # read-only snapshot before the external embedding/storage calls so a slow
    # provider cannot hold database locks or stale MVCC snapshots.
    db.rollback()
    unique_texts: list[str] = []
    seen_texts: set[str] = set()
    for kb_id in selected:
        for chunk in plan.get(kb_id, []):
            if chunk.text in seen_texts:
                continue
            seen_texts.add(chunk.text)
            unique_texts.append(chunk.text)

    per_kb: dict[str, dict[str, Any]] = {}
    nonempty_kb_ids: list[str] = []
    for kb_id in selected:
        chunks = plan.get(kb_id, [])
        if chunks:
            nonempty_kb_ids.append(kb_id)
            continue
        per_kb[kb_id] = _write_kb_partition(
            operation=operation,
            project_id=project_id,
            kb_id=kb_id,
            chunks=[],
            embeddings=[],
        )

    if not nonempty_kb_ids:
        return _summarize_results(
            operation=operation,
            selected=selected,
            per_kb=per_kb,
            unique_text_count=0,
        )

    enabled, disabled_reason = _vector_enabled_reason(embedding=embedding)
    if not enabled:
        count_key = "rebuilt" if operation == "rebuild" else "ingested"
        per_kb.update(
            {
                kb_id: {
                    "enabled": False,
                    "skipped": True,
                    "disabled_reason": disabled_reason,
                    count_key: 0,
                }
                for kb_id in nonempty_kb_ids
            }
        )
        return _summarize_results(
            operation=operation,
            selected=selected,
            per_kb=per_kb,
            unique_text_count=0,
        )

    embed_out = (
        embed_texts_with_providers(unique_texts, embedding=embedding)
        if unique_texts
        else {"enabled": True, "vectors": []}
    )
    if not bool(embed_out.get("enabled")):
        reason = str(embed_out.get("disabled_reason") or "error")
        count_key = "rebuilt" if operation == "rebuild" else "ingested"
        per_kb.update(
            {
                kb_id: {
                    "enabled": False,
                    "skipped": True,
                    "disabled_reason": reason,
                    "error": embed_out.get("error"),
                    count_key: 0,
                }
                for kb_id in nonempty_kb_ids
            }
        )
        return _summarize_results(
            operation=operation,
            selected=selected,
            per_kb=per_kb,
            unique_text_count=len(unique_texts),
        )

    vectors = list(embed_out.get("vectors") or [])
    if len(vectors) != len(unique_texts):
        raise ValueError("embedding provider returned an unexpected vector count")
    vector_by_text = dict(zip(unique_texts, vectors, strict=True))

    for kb_id in nonempty_kb_ids:
        chunks = plan.get(kb_id, [])
        embeddings = [vector_by_text[chunk.text] for chunk in chunks]
        per_kb[kb_id] = _write_kb_partition(
            operation=operation,
            project_id=project_id,
            kb_id=kb_id,
            chunks=chunks,
            embeddings=embeddings,
        )

    return _summarize_results(
        operation=operation,
        selected=selected,
        per_kb=per_kb,
        unique_text_count=len(unique_texts),
    )


def rebuild_kb_vectors(
    *,
    db: Session,
    project_id: str,
    kb_ids: list[str],
    embedding: dict[str, str | None] | None,
    sources: list[VectorSource] | None = None,
) -> dict[str, Any]:
    return run_kb_vector_operation(
        db=db,
        project_id=project_id,
        kb_ids=kb_ids,
        embedding=embedding,
        operation="rebuild",
        sources=sources,
    )


def ingest_kb_vectors(
    *,
    db: Session,
    project_id: str,
    kb_ids: list[str],
    embedding: dict[str, str | None] | None,
    sources: list[VectorSource] | None = None,
) -> dict[str, Any]:
    return run_kb_vector_operation(
        db=db,
        project_id=project_id,
        kb_ids=kb_ids,
        embedding=embedding,
        operation="ingest",
        sources=sources,
    )
