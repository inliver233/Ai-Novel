from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import exception_log_fields, log_event
from app.db.session import SessionLocal, engine
from app.services.embedding_service import (
    embed_texts as embed_texts_with_providers,
    embedding_enabled_reason,
    resolve_embedding_config,
)
from app.services.vector_chroma_fallback import (
    _INMEMORY_CHROMA,
    _INMEMORY_CHROMADB,
    _InMemoryChromaModule,
    _InMemoryClient,
    _InMemoryCollection,
    _cosine_distance,
    load_chromadb_or_fallback as _import_chromadb,
)
from app.services.vector_types import VectorChunk, VectorSource, _ALL_SOURCES

logger = logging.getLogger("ainovel")
_PGVECTOR_TABLE = "vector_chunks"
# (ready, probed_at, ttl_seconds)：探活异常只做短负缓存，避免瞬时故障被钉死为持续降级。
_PGVECTOR_READY_CACHE: tuple[bool, float, float] | None = None
_PGVECTOR_READY_CACHE_TTL_SECONDS = 30.0
_PGVECTOR_READY_PROBE_FAILURE_TTL_SECONDS = 5.0


def _is_postgres() -> bool:
    return getattr(getattr(engine, "dialect", None), "name", "") == "postgresql"


def _pgvector_ready() -> bool:
    global _PGVECTOR_READY_CACHE
    now = time.time()
    cached = _PGVECTOR_READY_CACHE
    if cached is not None and (now - cached[1]) < cached[2]:
        return bool(cached[0])

    if not _is_postgres():
        _PGVECTOR_READY_CACHE = (False, now, _PGVECTOR_READY_CACHE_TTL_SECONDS)
        return False

    ready = False
    ttl = _PGVECTOR_READY_CACHE_TTL_SECONDS
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
    except Exception as exc:
        ready = False
        ttl = _PGVECTOR_READY_PROBE_FAILURE_TTL_SECONDS
        log_event(
            logger,
            "warning",
            event="VECTOR_RAG",
            action="pgvector_ready_probe",
            backend="pgvector",
            ready=False,
            **exception_log_fields(exc),
        )

    _PGVECTOR_READY_CACHE = (bool(ready), now, ttl)
    return bool(ready)


def _prefer_pgvector() -> bool:
    backend = str(getattr(settings, "vector_backend", "auto") or "auto").strip().lower()
    if backend == "chroma":
        return False
    if backend == "pgvector":
        # 显式选择 pgvector 时禁止静默回落 Chroma（backend-data#4 fail-closed 语义）。
        if not _pgvector_ready():
            log_event(
                logger,
                "warning",
                event="VECTOR_RAG",
                action="backend_select",
                backend="pgvector",
                ready=False,
            )
            raise AppError(
                code="PGVECTOR_BACKEND_UNAVAILABLE",
                message="pgvector 后端不可用：请确认数据库为 PostgreSQL、已安装 vector 扩展并完成迁移，或将 VECTOR_BACKEND 改为 auto/chroma",
                status_code=503,
                details={"configured_backend": "pgvector"},
            )
        return True
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


def _normalize_kb_id(kb_id: str | None) -> str:
    raw = str(kb_id or "").strip()
    return raw or "default"


def _validate_embedding_dimensions(embeddings: list[list[float]], *, require_pg_dimension: bool) -> None:
    dimensions = [len(vector) for vector in embeddings]
    if any(dimension <= 0 for dimension in dimensions) or len(set(dimensions)) > 1:
        raise AppError(
            code="EMBEDDING_DIMENSION_MISMATCH",
            message="Embedding 批次向量维度不一致",
            status_code=422,
            details={"dimensions": dimensions},
        )
    if require_pg_dimension and dimensions and dimensions[0] != 1536:
        raise AppError(
            code="PGVECTOR_DIMENSION_UNSUPPORTED",
            message="PostgreSQL pgvector 后端仅支持 1536 维 embedding",
            status_code=422,
            details={"actual_dimension": dimensions[0], "supported_dimension": 1536},
        )


def _collection_embedding_dimension(collection: Any) -> int | None:
    snapshot = collection.get(include=["embeddings"], limit=1)
    embeddings = snapshot.get("embeddings")
    if embeddings is None or len(embeddings) == 0:
        return None
    return len(embeddings[0])


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


def _pgvector_upsert_chunks_in_session(
    *, db: Any, project_id: str, kb_id: str | None, chunks: list[VectorChunk], embeddings: list[list[float]]
) -> int:
    kb = _normalize_kb_id(kb_id)
    sql = text(
        """
        INSERT INTO vector_chunks (
            id,
            project_id,
            kb_id,
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
            :kb_id,
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
        ON CONFLICT (project_id, kb_id, id) DO UPDATE SET
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
                "kb_id": kb,
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
        return 0

    db.execute(sql, params)
    return len(params)


def _pgvector_upsert_chunks(
    *, project_id: str, kb_id: str | None = None, chunks: list[VectorChunk], embeddings: list[list[float]]
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        ingested = _pgvector_upsert_chunks_in_session(
            db=db,
            project_id=project_id,
            kb_id=kb_id,
            chunks=chunks,
            embeddings=embeddings,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"enabled": True, "skipped": False, "ingested": ingested}


def _pgvector_delete_project(*, project_id: str, kb_id: str | None = None) -> None:
    db = SessionLocal()
    try:
        params: dict[str, Any] = {"project_id": project_id}
        where_sql = "project_id = :project_id"
        if kb_id is not None:
            where_sql += " AND kb_id = :kb_id"
            params["kb_id"] = _normalize_kb_id(kb_id)
        db.execute(text(f"DELETE FROM vector_chunks WHERE {where_sql}"), params)
        db.commit()
    finally:
        db.close()


def _pgvector_hybrid_fetch(
    *,
    project_id: str,
    kb_id: str | None = None,
    query_text: str,
    query_vec: list[float],
    sources: list[VectorSource],
    vector_k: int,
    fts_k: int,
    rrf_k: int,
) -> dict[str, Any]:
    qvec = _pgvector_literal(query_vec)
    qtext = (query_text or "").strip() or " "

    kb = _normalize_kb_id(kb_id)
    where_sql = "project_id = :project_id AND kb_id = :kb_id"
    is_cjk = bool(re.search(r"[\u3400-\u9fff]", qtext))
    escaped = qtext.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    base_params: dict[str, Any] = {
        "project_id": project_id,
        "kb_id": kb,
        "qvec": qvec,
        "qtext": qtext,
        "qpattern": f"%{escaped}%",
    }
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
    if is_cjk:
        lexical_score_sql = (
            "CASE WHEN text_md ILIKE :qpattern ESCAPE '\\' THEN 1.0 "
            "ELSE GREATEST(similarity(text_md, :qtext), word_similarity(:qtext, text_md)) END"
        )
        lexical_where_sql = "(text_md ILIKE :qpattern ESCAPE '\\' OR text_md %> :qtext)"
    else:
        lexical_score_sql = "ts_rank_cd(content_tsv, plainto_tsquery('simple', :qtext))"
        lexical_where_sql = "content_tsv @@ plainto_tsquery('simple', :qtext)"
    fts_sql = text(
        f"""
        SELECT id, {lexical_score_sql} AS score
        FROM {_PGVECTOR_TABLE}
        WHERE {where_sql} AND {lexical_where_sql}
        ORDER BY score DESC, id ASC
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
                {lexical_score_sql} AS fts_score
            FROM {_PGVECTOR_TABLE}
            WHERE project_id = :project_id
              AND kb_id = :kb_id
              AND id = ANY((:ids)::text[])
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
        meta["kb_id"] = kb
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


def _pgvector_hybrid_query(
    *, project_id: str, kb_id: str | None = None, query_text: str, query_vec: list[float], sources: list[VectorSource]
) -> dict[str, Any]:
    if not _is_postgres():
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
        out = _pgvector_hybrid_fetch(
            project_id=project_id,
            kb_id=kb_id,
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


def ingest_chunks_with_embeddings(
    *,
    project_id: str,
    kb_id: str | None = None,
    chunks: list[VectorChunk],
    embeddings: list[list[float]],
) -> dict[str, Any]:
    if len(chunks) != len(embeddings):
        raise ValueError("chunks and embeddings must have identical lengths")
    _validate_embedding_dimensions(embeddings, require_pg_dimension=_prefer_pgvector())
    if not chunks:
        return {
            "enabled": True,
            "skipped": False,
            "ingested": 0,
            "backend": "pgvector" if _prefer_pgvector() else "chroma",
            "timings_ms": {"upsert": 0},
        }
    texts = [c.text for c in chunks]
    ids = [c.id for c in chunks]
    metadatas = [c.metadata for c in chunks]

    if _prefer_pgvector():
        try:
            write_start = time.perf_counter()
            out = _pgvector_upsert_chunks(project_id=project_id, kb_id=kb_id, chunks=chunks, embeddings=embeddings)
            write_ms = int((time.perf_counter() - write_start) * 1000)
            log_event(
                logger,
                "info",
                event="VECTOR_RAG",
                action="ingest",
                project_id=project_id,
                chunks=len(chunks),
                timings_ms={"upsert": write_ms},
                backend="pgvector",
            )
            return {**out, "timings_ms": {"upsert": write_ms}, "backend": "pgvector"}
        except AppError:
            raise
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
            raise AppError(
                code="PGVECTOR_WRITE_FAILED",
                message="pgvector 写入失败",
                status_code=500,
                details={"error_type": type(exc).__name__},
            ) from exc

    try:
        collection = _get_collection(project_id=project_id, kb_id=kb_id)
    except Exception as exc:  # pragma: no cover - env dependent
        return {
            "enabled": False,
            "skipped": True,
            "disabled_reason": "chroma_unavailable",
            "error": str(exc),
            "ingested": 0,
        }

    existing_dimension = _collection_embedding_dimension(collection) if embeddings else None
    if existing_dimension is not None and embeddings and existing_dimension != len(embeddings[0]):
        raise AppError(
            code="CHROMA_DIMENSION_MISMATCH",
            message="Chroma collection 向量维度与当前 embedding 配置不一致，请先重建索引",
            status_code=409,
            details={"existing_dimension": existing_dimension, "actual_dimension": len(embeddings[0])},
        )
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
        timings_ms={"upsert": write_ms},
        backend="chroma",
    )
    return {
        "enabled": True,
        "skipped": False,
        "ingested": len(chunks),
        "timings_ms": {"upsert": write_ms},
        "backend": "chroma",
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
    texts = [chunk.text for chunk in chunks]
    embed_out = embed_texts_with_providers(texts, embedding=embedding) if texts else {"enabled": True, "vectors": []}
    if not bool(embed_out.get("enabled")):
        disabled = str(embed_out.get("disabled_reason") or "error")
        return {
            "enabled": False,
            "skipped": True,
            "disabled_reason": disabled,
            "error": embed_out.get("error"),
            "ingested": 0,
        }
    embed_ms = int((time.perf_counter() - start) * 1000)
    out = ingest_chunks_with_embeddings(
        project_id=project_id,
        kb_id=kb_id,
        chunks=chunks,
        embeddings=embed_out.get("vectors") or [],
    )
    timings = dict(out.get("timings_ms") or {})
    timings["embed"] = embed_ms
    return {**out, "timings_ms": timings}


def rebuild_project_with_embeddings(
    *,
    project_id: str,
    kb_id: str | None = None,
    chunks: list[VectorChunk],
    embeddings: list[list[float]],
) -> dict[str, Any]:
    if len(chunks) != len(embeddings):
        raise ValueError("chunks and embeddings must have identical lengths")
    _validate_embedding_dimensions(embeddings, require_pg_dimension=_prefer_pgvector())

    if _prefer_pgvector():
        try:
            kb = _normalize_kb_id(kb_id)
            db = SessionLocal()
            try:
                db.execute(
                    text("DELETE FROM vector_chunks WHERE project_id = :project_id AND kb_id = :kb_id"),
                    {"project_id": project_id, "kb_id": kb},
                )
                if chunks:
                    _pgvector_upsert_chunks_in_session(
                        db=db,
                        project_id=project_id,
                        kb_id=kb,
                        chunks=chunks,
                        embeddings=embeddings,
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
            return {
                "enabled": True,
                "skipped": False,
                "rebuilt": len(chunks),
                "ingested": len(chunks),
                "backend": "pgvector",
            }
        except AppError:
            raise
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
            raise AppError(
                code="PGVECTOR_REBUILD_FAILED",
                message="pgvector 重建失败",
                status_code=500,
                details={"error_type": type(exc).__name__},
            ) from exc

    snapshots: dict[str, dict[str, Any]] = {}
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
                old_collection = client.get_collection(name=name)
                snapshot = old_collection.get(include=["documents", "metadatas", "embeddings"])
                snapshots[name] = {
                    "ids": list(snapshot.get("ids") or []),
                    "documents": list(snapshot.get("documents") or []),
                    "metadatas": list(snapshot.get("metadatas") or []),
                    "embeddings": [
                        [float(value) for value in vector]
                        for vector in (snapshot.get("embeddings") if snapshot.get("embeddings") is not None else [])
                    ],
                    "metadata": dict(
                        getattr(old_collection, "metadata", None) or getattr(old_collection, "_metadata", None) or {}
                    ),
                }
            except Exception:
                continue
        for name in names:
            try:
                client.delete_collection(name=name)
            except Exception:
                pass
    except Exception as exc:  # pragma: no cover - env dependent
        return {
            "enabled": False,
            "skipped": True,
            "disabled_reason": "chroma_unavailable",
            "error": str(exc),
            "rebuilt": 0,
        }

    try:
        if chunks:
            out = ingest_chunks_with_embeddings(
                project_id=project_id,
                kb_id=kb_id,
                chunks=chunks,
                embeddings=embeddings,
            )
            if not bool(out.get("enabled")) or bool(out.get("skipped")):
                raise RuntimeError(str(out.get("error") or out.get("disabled_reason") or "chroma replace failed"))
            return {
                "enabled": bool(out.get("enabled")),
                "skipped": bool(out.get("skipped")),
                "rebuilt": int(out.get("ingested") or 0),
                **out,
            }
        return {
            "enabled": True,
            "skipped": False,
            "rebuilt": 0,
            "ingested": 0,
            "backend": "chroma",
        }
    except Exception as exc:  # pragma: no cover - concrete rollback paths are tested
        restore_error: Exception | None = None
        try:
            for name in names:
                try:
                    client.delete_collection(name=name)
                except Exception:
                    pass
            # Chroma has no multi-operation transaction. This snapshot restores
            # runtime exceptions; an abrupt process/host crash between delete
            # and restore remains outside the guarantees of PersistentClient.
            for name, snapshot in snapshots.items():
                collection = client.get_or_create_collection(
                    name=name,
                    metadata=snapshot["metadata"] or None,
                )
                if snapshot["ids"]:
                    collection.upsert(
                        ids=snapshot["ids"],
                        documents=snapshot["documents"],
                        metadatas=snapshot["metadatas"],
                        embeddings=snapshot["embeddings"],
                    )
        except Exception as restore_exc:  # pragma: no cover - storage failure
            restore_error = restore_exc
        return {
            "enabled": True,
            "skipped": True,
            "disabled_reason": "chroma_rebuild_failed",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "restored": restore_error is None,
            "restore_error": str(restore_error) if restore_error is not None else None,
            "rebuilt": 0,
            "backend": "chroma",
        }


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

    texts = [chunk.text for chunk in chunks]
    embed_out = embed_texts_with_providers(texts, embedding=embedding) if texts else {"enabled": True, "vectors": []}
    if not bool(embed_out.get("enabled")):
        return {
            "enabled": False,
            "skipped": True,
            "disabled_reason": str(embed_out.get("disabled_reason") or "error"),
            "error": embed_out.get("error"),
            "rebuilt": 0,
        }
    return rebuild_project_with_embeddings(
        project_id=project_id,
        kb_id=kb_id,
        chunks=chunks,
        embeddings=embed_out.get("vectors") or [],
    )


def purge_document_vectors(
    *,
    project_id: str,
    kb_id: str | None,
    document_id: str,
) -> dict[str, Any]:
    """Delete only one imported document's vectors from its owning KB."""

    kb = _normalize_kb_id(kb_id)
    doc_id = str(document_id or "").strip()
    if not doc_id:
        return {"enabled": True, "skipped": True, "deleted": 0, "reason": "document_id_missing"}

    if _prefer_pgvector():
        db = SessionLocal()
        try:
            result = db.execute(
                text(
                    "DELETE FROM vector_chunks "
                    "WHERE project_id = :project_id AND kb_id = :kb_id AND source_id = :document_id"
                ),
                {"project_id": project_id, "kb_id": kb, "document_id": doc_id},
            )
            db.commit()
            return {
                "enabled": True,
                "skipped": False,
                "deleted": int(getattr(result, "rowcount", 0) or 0),
                "backend": "pgvector",
            }
        except Exception as exc:  # pragma: no cover - environment dependent
            db.rollback()
            return {
                "enabled": True,
                "skipped": True,
                "deleted": 0,
                "backend": "pgvector",
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
        finally:
            db.close()

    try:
        collection = _get_collection(project_id=project_id, kb_id=kb)
        # ``source_id`` also matches vectors written before origin metadata was
        # introduced, so retry cleanup remains backwards compatible.
        collection.delete(where={"source_id": doc_id})
        return {"enabled": True, "skipped": False, "deleted": True, "backend": "chroma"}
    except Exception as exc:  # pragma: no cover - environment dependent
        return {
            "enabled": False,
            "skipped": True,
            "deleted": False,
            "backend": "chroma",
            "error": str(exc),
            "error_type": type(exc).__name__,
        }


def purge_project_vectors(*, project_id: str, kb_id: str | None = None) -> dict[str, Any]:
    """
    Best-effort deletion of vector index data for the given project.

    - Postgres: delete rows in vector_chunks (pgvector backend).
    - SQLite: delete Chroma collection (if chromadb is installed).
    """
    t0 = time.perf_counter()

    if _prefer_pgvector():
        try:
            _pgvector_delete_project(project_id=project_id, kb_id=kb_id)
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
