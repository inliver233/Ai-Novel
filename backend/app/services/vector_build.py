from __future__ import annotations

import json
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import exception_log_fields, log_event
from app.db.session import SessionLocal, engine
from app.db.utils import new_id, utc_now
from app.models.chapter import Chapter
from app.models.outline import Outline
from app.models.project_settings import ProjectSettings
from app.models.project_task import ProjectTask
from app.models.story_memory import StoryMemory
from app.services.embedding_service import (
    embed_texts as embed_texts_with_providers,
    embedding_enabled_reason,
    resolve_embedding_config,
)
from app.services.project_task_event_service import emit_and_enqueue_project_task, reset_project_task_to_queued
from app.services.vector_chroma_fallback import (
    _INMEMORY_CHROMA,
    _INMEMORY_CHROMADB,
    _InMemoryChromaModule,
    _InMemoryClient,
    _InMemoryCollection,
    _cosine_distance,
    load_chromadb_or_fallback as _import_chromadb,
)

logger = logging.getLogger("ainovel")
VectorSource = Literal["worldbook", "outline", "chapter", "story_memory"]


@dataclass(frozen=True, slots=True)
class VectorChunk:
    id: str
    text: str
    metadata: dict[str, Any]


_ALL_SOURCES: list[VectorSource] = ["worldbook", "outline", "chapter", "story_memory"]
_PGVECTOR_TABLE = "vector_chunks"
_PGVECTOR_READY_CACHE: tuple[bool, float] | None = None
_PGVECTOR_READY_CACHE_TTL_SECONDS = 30.0

def _is_postgres() -> bool:
    return getattr(getattr(engine, "dialect", None), "name", "") == "postgresql"


def _pgvector_ready() -> bool:
    global _PGVECTOR_READY_CACHE
    now = time.time()
    cached = _PGVECTOR_READY_CACHE
    if cached is not None and (now - cached[1]) < _PGVECTOR_READY_CACHE_TTL_SECONDS:
        return bool(cached[0])

    if not _is_postgres():
        _PGVECTOR_READY_CACHE = (False, now)
        return False

    ready = False
    try:
        with engine.connect() as conn:
            ext_installed = bool(conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).scalar())
            if not ext_installed:
                ready = False
            else:
                table_exists = bool(
                    conn.execute(text("SELECT to_regclass('public.vector_chunks') IS NOT NULL")).scalar()
                )
                ready = bool(table_exists)
    except Exception:
        ready = False

    _PGVECTOR_READY_CACHE = (bool(ready), now)
    return bool(ready)


def _prefer_pgvector() -> bool:
    backend = str(getattr(settings, "vector_backend", "auto") or "auto").strip().lower()
    if backend == "chroma":
        return False
    if backend == "pgvector":
        return _pgvector_ready()
    return _pgvector_ready()


def _safe_json_loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}

def _pgvector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{float(x):.8f}" for x in vec) + "]"


def _rrf_contrib(rank: int | None, *, k: int) -> float:
    if rank is None or rank <= 0:
        return 0.0
    return 1.0 / (k + rank)


def _rrf_score(*, vector_rank: int | None, fts_rank: int | None, k: int) -> float:
    return _rrf_contrib(vector_rank, k=k) + _rrf_contrib(fts_rank, k=k)

def _backend_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_chroma_persist_dir() -> str:
    return str((_backend_dir() / ".chroma").resolve().as_posix())


def _vector_enabled_reason(*, embedding: dict[str, str | None] | None = None) -> tuple[bool, str | None]:
    config = resolve_embedding_config(embedding)
    return embedding_enabled_reason(config)

def schedule_vector_rebuild_task(
    *,
    db: Session | None = None,
    project_id: str,
    actor_user_id: str | None,
    request_id: str | None,
    reason: str,
) -> str | None:
    """
    Fail-soft scheduler: ensure/enqueue a ProjectTask(kind=vector_rebuild) for the project.

    Idempotency key is derived from `ProjectSettings.last_vector_build_at` to avoid task storms while still allowing
    a new task after each successful rebuild.
    """

    pid = str(project_id or "").strip()
    if not pid:
        return None
    reason_norm = str(reason or "").strip() or "dirty"
    owns_session = db is None
    if db is None:
        db = SessionLocal()
    try:
        settings_row = db.get(ProjectSettings, pid)
        if settings_row is not None and not bool(getattr(settings_row, "vector_index_dirty", False)):
            return None

        last_build_at = getattr(settings_row, "last_vector_build_at", None) if settings_row is not None else None
        token = "none"
        if last_build_at is not None:
            token = last_build_at.isoformat().replace("+00:00", "Z")

        idempotency_key = f"vector:project:since:{token}:v1"
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
                kind="vector_rebuild",
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
            event="VECTOR_REBUILD_SCHEDULE_ERROR",
            project_id=pid,
            error_type=type(exc).__name__,
            request_id=request_id,
            **exception_log_fields(exc),
        )
        return None
    finally:
        if owns_session:
            db.close()


def _normalize_kb_id(kb_id: str | None) -> str:
    raw = str(kb_id or "").strip()
    return raw or "default"


def _legacy_collection_name(project_id: str) -> str:
    raw = f"ainovel_{project_id}"
    safe = re.sub(r"[^A-Za-z0-9_\\-]+", "_", raw).strip("_")
    if not safe:
        safe = "ainovel_default"
    return safe[:60]


def _hash_collection_name(project_id: str, kb_id: str | None = None) -> str:
    kb = _normalize_kb_id(kb_id)
    digest = hashlib.sha256(f"{project_id}:{kb}".encode("utf-8")).hexdigest()[:24]
    return f"ainovel_{digest}"


def _chroma_collection_naming() -> str:
    raw = str(getattr(settings, "vector_chroma_collection_naming", "legacy") or "legacy").strip().lower()
    return raw if raw in ("legacy", "hash") else "legacy"


def _migrate_chroma_collection(*, source: Any, target: Any) -> int:
    migrated = 0
    offset = 0
    limit = 1000
    while True:
        batch = source.get(
            include=["documents", "metadatas", "embeddings"],
            limit=limit,
            offset=offset,
        )
        ids = batch.get("ids") or []
        if not ids:
            break
        target.upsert(
            ids=ids,
            documents=batch.get("documents"),
            metadatas=batch.get("metadatas"),
            embeddings=batch.get("embeddings"),
        )
        migrated += len(ids)
        offset += len(ids)
    return migrated


def _get_collection(*, project_id: str, kb_id: str | None = None):
    chromadb = _import_chromadb()
    persist_dir = settings.vector_chroma_persist_dir or _default_chroma_persist_dir()
    client = chromadb.PersistentClient(path=persist_dir)

    kb = _normalize_kb_id(kb_id)
    legacy_name = _legacy_collection_name(project_id)
    hash_name = _hash_collection_name(project_id, kb)

    naming = _chroma_collection_naming()
    if naming == "legacy" and kb == "default":
        return client.get_or_create_collection(
            name=legacy_name,
            metadata={"project_id": project_id, "kb_id": kb, "naming": "legacy"},
        )

    if kb != "default":
        return client.get_or_create_collection(
            name=hash_name,
            metadata={"project_id": project_id, "kb_id": kb, "naming": "hash"},
        )

    try:
        return client.get_collection(name=hash_name)
    except Exception:
        pass

    try:
        legacy_collection = client.get_collection(name=legacy_name)
    except Exception:
        legacy_collection = None

    if legacy_collection is None:
        return client.get_or_create_collection(
            name=hash_name,
            metadata={"project_id": project_id, "kb_id": kb, "naming": "hash"},
        )

    t0 = time.perf_counter()
    migrated = 0
    try:
        hash_collection = client.get_or_create_collection(
            name=hash_name,
            metadata={"project_id": project_id, "kb_id": kb, "naming": "hash", "migrated_from": legacy_name},
        )
        migrated = _migrate_chroma_collection(source=legacy_collection, target=hash_collection)
        try:
            client.delete_collection(name=legacy_name)
        except Exception as exc:  # pragma: no cover - env dependent
            log_event(
                logger,
                "warning",
                event="VECTOR_RAG",
                action="collection_migrate_cleanup",
                project_id=project_id,
                backend="chroma",
                from_collection=legacy_name,
                to_collection=hash_name,
                migrated=migrated,
                error_type=type(exc).__name__,
                **exception_log_fields(exc),
            )
        log_event(
            logger,
            "info",
            event="VECTOR_RAG",
            action="collection_migrate",
            project_id=project_id,
            backend="chroma",
            from_collection=legacy_name,
            to_collection=hash_name,
            migrated=migrated,
            timings_ms={"total": int((time.perf_counter() - t0) * 1000)},
        )
        return hash_collection
    except Exception as exc:  # pragma: no cover - env dependent
        try:
            client.delete_collection(name=hash_name)
        except Exception:
            pass
        log_event(
            logger,
            "warning",
            event="VECTOR_RAG",
            action="collection_migrate",
            project_id=project_id,
            backend="chroma",
            from_collection=legacy_name,
            to_collection=hash_name,
            migrated=migrated,
            error_type=type(exc).__name__,
            **exception_log_fields(exc),
            timings_ms={"total": int((time.perf_counter() - t0) * 1000)},
        )
        return legacy_collection


def _pgvector_upsert_chunks(*, project_id: str, chunks: list[VectorChunk], embeddings: list[list[float]]) -> dict[str, Any]:
    sql = text(
        """
        INSERT INTO vector_chunks (
            id,
            project_id,
            source,
            source_id,
            chunk_index,
            title,
            chapter_number,
            text_md,
            metadata_json,
            embedding,
            updated_at
        ) VALUES (
            :id,
            :project_id,
            :source,
            :source_id,
            :chunk_index,
            :title,
            :chapter_number,
            :text_md,
            :metadata_json,
            (:embedding)::vector,
            NOW()
        )
        ON CONFLICT (id) DO UPDATE SET
            project_id = EXCLUDED.project_id,
            source = EXCLUDED.source,
            source_id = EXCLUDED.source_id,
            chunk_index = EXCLUDED.chunk_index,
            title = EXCLUDED.title,
            chapter_number = EXCLUDED.chapter_number,
            text_md = EXCLUDED.text_md,
            metadata_json = EXCLUDED.metadata_json,
            embedding = EXCLUDED.embedding,
            updated_at = NOW()
        """.strip()
    )

    params: list[dict[str, Any]] = []
    for c, emb in zip(chunks, embeddings):
        meta = c.metadata if isinstance(c.metadata, dict) else {}
        source = str(meta.get("source") or "")
        source_id = str(meta.get("source_id") or "")
        try:
            chunk_index = int(meta.get("chunk_index") or 0)
        except Exception:
            chunk_index = 0
        title = str(meta.get("title") or "").strip() or None
        chapter_number = meta.get("chapter_number")
        try:
            chapter_number_int = int(chapter_number) if chapter_number is not None else None
        except Exception:
            chapter_number_int = None

        params.append(
            {
                "id": c.id,
                "project_id": project_id,
                "source": source,
                "source_id": source_id,
                "chunk_index": chunk_index,
                "title": title,
                "chapter_number": chapter_number_int,
                "text_md": c.text,
                "metadata_json": json.dumps(meta, ensure_ascii=False),
                "embedding": _pgvector_literal([float(x) for x in emb]),
            }
        )

    if not params:
        return {"enabled": True, "skipped": False, "ingested": 0}

    db = SessionLocal()
    try:
        db.execute(sql, params)
        db.commit()
    finally:
        db.close()
    return {"enabled": True, "skipped": False, "ingested": len(params)}


def _pgvector_delete_project(*, project_id: str) -> None:
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM vector_chunks WHERE project_id = :project_id"), {"project_id": project_id})
        db.commit()
    finally:
        db.close()


def _pgvector_hybrid_fetch(
    *,
    project_id: str,
    query_text: str,
    query_vec: list[float],
    sources: list[VectorSource],
    vector_k: int,
    fts_k: int,
    rrf_k: int,
) -> dict[str, Any]:
    qvec = _pgvector_literal(query_vec)
    qtext = (query_text or "").strip() or " "

    where_sql = "project_id = :project_id"
    base_params: dict[str, Any] = {"project_id": project_id, "qvec": qvec, "qtext": qtext}
    if len(sources) == 1:
        where_sql += " AND source = :source"
        base_params["source"] = sources[0]
    elif sources:
        where_sql += " AND source = ANY((:sources)::text[])"
        base_params["sources"] = sources

    vec_sql = text(
        f"""
        SELECT id, (embedding <=> (:qvec)::vector) AS distance
        FROM {_PGVECTOR_TABLE}
        WHERE {where_sql}
        ORDER BY embedding <=> (:qvec)::vector ASC
        LIMIT :limit
        """.strip()
    )
    fts_sql = text(
        f"""
        SELECT id, ts_rank_cd(content_tsv, plainto_tsquery('simple', :qtext)) AS score
        FROM {_PGVECTOR_TABLE}
        WHERE {where_sql} AND content_tsv @@ plainto_tsquery('simple', :qtext)
        ORDER BY score DESC
        LIMIT :limit
        """.strip()
    )

    db = SessionLocal()
    try:
        vec_rows = db.execute(vec_sql, {**base_params, "limit": int(vector_k)}).all()
        fts_rows = db.execute(fts_sql, {**base_params, "limit": int(fts_k)}).all()

        vec_ids = [str(r[0]) for r in vec_rows]
        fts_ids = [str(r[0]) for r in fts_rows]
        ids = list(dict.fromkeys([*vec_ids, *fts_ids]).keys())
        if not ids:
            return {
                "candidates": [],
                "ranks": {"vector": {}, "fts": {}, "rrf_k": int(rrf_k)},
                "counts": {"vector": 0, "fts": 0, "union": 0},
            }

        vec_ranks = {cid: i + 1 for i, cid in enumerate(vec_ids)}
        fts_ranks = {cid: i + 1 for i, cid in enumerate(fts_ids)}

        details_sql = text(
            f"""
            SELECT
                id,
                text_md,
                metadata_json,
                (embedding <=> (:qvec)::vector) AS distance,
                ts_rank_cd(content_tsv, plainto_tsquery('simple', :qtext)) AS fts_score
            FROM {_PGVECTOR_TABLE}
            WHERE id = ANY((:ids)::text[])
            """.strip()
        )
        rows = db.execute(details_sql, {**base_params, "ids": ids}).all()
    finally:
        db.close()

    candidates: list[dict[str, Any]] = []
    for r in rows:
        cid = str(r[0])
        text_md = str(r[1] or "")
        meta = _safe_json_loads(str(r[2] or ""))
        try:
            distance = float(r[3])
        except Exception:
            distance = 0.0
        try:
            fts_score = float(r[4]) if r[4] is not None else 0.0
        except Exception:
            fts_score = 0.0

        vrank = vec_ranks.get(cid)
        frank = fts_ranks.get(cid)
        rrf_score = _rrf_score(vector_rank=vrank, fts_rank=frank, k=int(rrf_k))

        hybrid_meta = {
            "vector_rank": vrank,
            "fts_rank": frank,
            "rrf_k": int(rrf_k),
            "rrf_score": rrf_score,
            "fts_score": fts_score,
        }
        if isinstance(meta.get("hybrid"), dict):
            meta["hybrid"] = {**(meta.get("hybrid") or {}), **hybrid_meta}
        else:
            meta["hybrid"] = hybrid_meta

        candidates.append(
            {
                "id": cid,
                "distance": distance,
                "text": text_md,
                "metadata": meta,
                "hybrid": hybrid_meta,
                "_rrf_score": rrf_score,
            }
        )

    candidates.sort(key=lambda c: (-float(c.get("_rrf_score") or 0.0), float(c.get("distance") or 0.0)))

    return {
        "candidates": candidates,
        "ranks": {"vector": vec_ranks, "fts": fts_ranks, "rrf_k": int(rrf_k)},
        "counts": {"vector": len(vec_rows), "fts": len(fts_rows), "union": len(ids)},
    }


def _pgvector_hybrid_query(*, project_id: str, query_text: str, query_vec: list[float], sources: list[VectorSource]) -> dict[str, Any]:
    # Backward-compat for tests/monkeypatch: allow overriding _is_postgres/_pgvector_hybrid_fetch via vector_rag_service.
    from app.services import vector_rag_service as _hub  # noqa: PLC0415

    if not _hub._is_postgres():
        raise RuntimeError("not_postgres")

    top_k = int(settings.vector_max_candidates or 20)
    rrf_k = int(settings.vector_hybrid_rrf_k or 60)
    vec_k = top_k
    fts_k = top_k

    overfilter_actions: list[str] = []
    requested_sources = list(sources or _ALL_SOURCES)
    used_sources = list(requested_sources)

    min_needed = max(1, min(3, int(settings.vector_final_max_chunks or 6)))
    for _attempt in range(3):
        out = _hub._pgvector_hybrid_fetch(
            project_id=project_id,
            query_text=query_text,
            query_vec=query_vec,
            sources=used_sources,
            vector_k=vec_k,
            fts_k=fts_k,
            rrf_k=rrf_k,
        )
        union_count = int(out.get("counts", {}).get("union") or 0)
        if not settings.vector_overfiltering_enabled:
            break
        if union_count >= min_needed:
            break
        if used_sources != _ALL_SOURCES:
            used_sources = list(_ALL_SOURCES)
            overfilter_actions.append("relax_sources")
            continue
        if vec_k <= top_k:
            vec_k = min(200, max(top_k * 3, top_k))
            fts_k = min(200, max(top_k * 3, top_k))
            overfilter_actions.append("expand_candidates")
            continue
        break

    return {
        **out,
        "overfilter": {
            "enabled": bool(settings.vector_overfiltering_enabled),
            "min_needed": min_needed,
            "requested_sources": requested_sources,
            "used_sources": used_sources,
            "actions": overfilter_actions,
            "vector_k": vec_k,
            "fts_k": fts_k,
        },
    }
def ingest_chunks(
    *,
    project_id: str,
    kb_id: str | None = None,
    chunks: list[VectorChunk],
    embedding: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    enabled, disabled_reason = _vector_enabled_reason(embedding=embedding)
    if not enabled:
        return {"enabled": False, "skipped": True, "disabled_reason": disabled_reason, "ingested": 0}

    start = time.perf_counter()
    texts = [c.text for c in chunks]
    ids = [c.id for c in chunks]
    metadatas = [c.metadata for c in chunks]

    embeddings: list[list[float]] = []
    if texts:
        embed_out = embed_texts_with_providers(texts, embedding=embedding)
        if not bool(embed_out.get("enabled")):
            disabled = str(embed_out.get("disabled_reason") or "error")
            log_event(
                logger,
                "warning",
                event="VECTOR_RAG",
                action="ingest",
                project_id=project_id,
                disabled_reason=disabled,
                error_type="EmbeddingError",
            )
            return {
                "enabled": False,
                "skipped": True,
                "disabled_reason": disabled,
                "error": embed_out.get("error"),
                "ingested": 0,
            }
        embeddings = embed_out.get("vectors") or []

    embed_ms = int((time.perf_counter() - start) * 1000)

    if _prefer_pgvector():
        try:
            write_start = time.perf_counter()
            out = _pgvector_upsert_chunks(project_id=project_id, chunks=chunks, embeddings=embeddings)
            write_ms = int((time.perf_counter() - write_start) * 1000)
            log_event(
                logger,
                "info",
                event="VECTOR_RAG",
                action="ingest",
                project_id=project_id,
                chunks=len(chunks),
                timings_ms={"embed": embed_ms, "upsert": write_ms},
                backend="pgvector",
            )
            return {**out, "timings_ms": {"embed": embed_ms, "upsert": write_ms}, "backend": "pgvector"}
        except Exception as exc:  # pragma: no cover - env dependent
            log_event(
                logger,
                "warning",
                event="VECTOR_RAG",
                action="ingest",
                project_id=project_id,
                backend="pgvector",
                fallback="chroma",
                error_type=type(exc).__name__,
            )

    try:
        collection = _get_collection(project_id=project_id, kb_id=kb_id)
    except Exception as exc:  # pragma: no cover - env dependent
        return {"enabled": False, "skipped": True, "disabled_reason": "chroma_unavailable", "error": str(exc), "ingested": 0}

    write_start = time.perf_counter()
    collection.upsert(ids=ids, documents=texts, metadatas=metadatas, embeddings=embeddings)
    write_ms = int((time.perf_counter() - write_start) * 1000)

    log_event(
        logger,
        "info",
        event="VECTOR_RAG",
        action="ingest",
        project_id=project_id,
        chunks=len(chunks),
        timings_ms={"embed": embed_ms, "upsert": write_ms},
        backend="chroma",
    )
    return {"enabled": True, "skipped": False, "ingested": len(chunks), "timings_ms": {"embed": embed_ms, "upsert": write_ms}, "backend": "chroma"}


def rebuild_project(
    *,
    project_id: str,
    kb_id: str | None = None,
    chunks: list[VectorChunk],
    embedding: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    enabled, disabled_reason = _vector_enabled_reason(embedding=embedding)
    if not enabled:
        return {"enabled": False, "skipped": True, "disabled_reason": disabled_reason, "rebuilt": 0}

    if _prefer_pgvector():
        try:
            _pgvector_delete_project(project_id=project_id)
        except Exception as exc:  # pragma: no cover - env dependent
            log_event(
                logger,
                "warning",
                event="VECTOR_RAG",
                action="rebuild",
                project_id=project_id,
                backend="pgvector",
                error_type=type(exc).__name__,
            )
        out = ingest_chunks(project_id=project_id, kb_id=kb_id, chunks=chunks, embedding=embedding)
        return {"enabled": bool(out.get("enabled")), "skipped": bool(out.get("skipped")), "rebuilt": int(out.get("ingested") or 0), **out}

    try:
        chromadb = _import_chromadb()
        persist_dir = settings.vector_chroma_persist_dir or _default_chroma_persist_dir()
        client = chromadb.PersistentClient(path=persist_dir)
        kb = _normalize_kb_id(kb_id)
        legacy_name = _legacy_collection_name(project_id)
        hash_name = _hash_collection_name(project_id, kb)
        naming = _chroma_collection_naming()
        if kb != "default":
            names = {hash_name}
        else:
            names = {legacy_name} if naming == "legacy" else {hash_name, legacy_name}
        for name in names:
            try:
                client.delete_collection(name=name)
            except Exception:
                pass
    except Exception as exc:  # pragma: no cover - env dependent
        return {"enabled": False, "skipped": True, "disabled_reason": "chroma_unavailable", "error": str(exc), "rebuilt": 0}

    out = ingest_chunks(project_id=project_id, kb_id=kb_id, chunks=chunks, embedding=embedding)
    return {"enabled": bool(out.get("enabled")), "skipped": bool(out.get("skipped")), "rebuilt": int(out.get("ingested") or 0), **out}


def purge_project_vectors(*, project_id: str, kb_id: str | None = None) -> dict[str, Any]:
    """
    Best-effort deletion of vector index data for the given project.

    - Postgres: delete rows in vector_chunks (pgvector backend).
    - SQLite: delete Chroma collection (if chromadb is installed).
    """
    t0 = time.perf_counter()

    if _prefer_pgvector():
        try:
            _pgvector_delete_project(project_id=project_id)
            out = {"enabled": True, "skipped": False, "deleted": True, "backend": "pgvector"}
            log_event(
                logger,
                "info",
                event="VECTOR_RAG",
                action="purge",
                project_id=project_id,
                backend="pgvector",
                deleted=True,
                timings_ms={"total": int((time.perf_counter() - t0) * 1000)},
            )
            out["timings_ms"] = {"total": int((time.perf_counter() - t0) * 1000)}
            return out
        except Exception as exc:  # pragma: no cover - env dependent
            log_event(
                logger,
                "warning",
                event="VECTOR_RAG",
                action="purge",
                project_id=project_id,
                backend="pgvector",
                deleted=False,
                error_type=type(exc).__name__,
                timings_ms={"total": int((time.perf_counter() - t0) * 1000)},
            )
            return {
                "enabled": True,
                "skipped": True,
                "deleted": False,
                "backend": "pgvector",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "timings_ms": {"total": int((time.perf_counter() - t0) * 1000)},
            }

    try:
        chromadb = _import_chromadb()
        persist_dir = settings.vector_chroma_persist_dir or _default_chroma_persist_dir()
        client = chromadb.PersistentClient(path=persist_dir)
        kb = _normalize_kb_id(kb_id)
        names = [_hash_collection_name(project_id, kb)]
        if kb == "default":
            names.append(_legacy_collection_name(project_id))
        delete_errors: list[str] = []
        delete_error_type: str | None = None
        deleted = True
        for name in names:
            try:
                client.delete_collection(name=name)
            except Exception as exc:  # pragma: no cover - env dependent
                msg = str(exc)
                msg_lower = msg.lower()
                if "does not exist" in msg_lower or "not found" in msg_lower:
                    continue
                deleted = False
                delete_errors.append(f"{name}: {msg}")
                delete_error_type = delete_error_type or type(exc).__name__

        error = "; ".join(delete_errors) if delete_errors else None
        error_type = delete_error_type

        out: dict[str, Any] = {
            "enabled": True,
            "skipped": False,
            "deleted": bool(deleted),
            "backend": "chroma",
            "timings_ms": {"total": int((time.perf_counter() - t0) * 1000)},
        }
        if error:
            out.update({"error": error, "error_type": error_type})
            log_event(
                logger,
                "warning",
                event="VECTOR_RAG",
                action="purge",
                project_id=project_id,
                backend="chroma",
                deleted=bool(deleted),
                error_type=error_type,
                timings_ms=out["timings_ms"],
            )
        else:
            log_event(
                logger,
                "info",
                event="VECTOR_RAG",
                action="purge",
                project_id=project_id,
                backend="chroma",
                deleted=True,
                timings_ms=out["timings_ms"],
            )
        return out
    except Exception as exc:  # pragma: no cover - env dependent
        log_event(
            logger,
            "warning",
            event="VECTOR_RAG",
            action="purge",
            project_id=project_id,
            backend="chroma",
            deleted=False,
            error_type=type(exc).__name__,
            timings_ms={"total": int((time.perf_counter() - t0) * 1000)},
        )
        return {
            "enabled": False,
            "skipped": True,
            "deleted": False,
            "backend": "chroma",
            "disabled_reason": "chroma_unavailable",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "timings_ms": {"total": int((time.perf_counter() - t0) * 1000)},
        }
