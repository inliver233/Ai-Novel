from __future__ import annotations

"""Process-local Chroma fallback shared by vector read and write paths."""

import logging
import math
import threading
from typing import Any

from app.core.logging import exception_log_fields, log_event


logger = logging.getLogger("ainovel")

_CHROMADB_FALLBACK_WARNING_LOCK = threading.Lock()
_CHROMADB_FALLBACK_WARNING_EMITTED = threading.Event()


def _load_chromadb() -> Any:
    import chromadb  # type: ignore[import-not-found]

    return chromadb


def _log_chromadb_fallback_once(exc: Exception) -> None:
    should_log = False
    with _CHROMADB_FALLBACK_WARNING_LOCK:
        if not _CHROMADB_FALLBACK_WARNING_EMITTED.is_set():
            _CHROMADB_FALLBACK_WARNING_EMITTED.set()
            should_log = True

    if should_log:
        log_event(
            logger,
            "warning",
            event="VECTOR_RAG",
            action="chromadb_import_fallback",
            backend="chroma",
            fallback_backend="inmemory",
            process_local=True,
            error_type=type(exc).__name__,
            **exception_log_fields(exc),
        )


def load_chromadb_or_fallback() -> Any:
    """Load Chroma or return the shared process-local in-memory implementation."""

    try:
        return _load_chromadb()
    except Exception as exc:  # pragma: no cover - availability is environment dependent
        _log_chromadb_fallback_once(exc)
        return _INMEMORY_CHROMADB


def _cosine_distance(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 1.0
    n = min(len(a), len(b))
    if n <= 0:
        return 1.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for i in range(n):
        av = float(a[i])
        bv = float(b[i])
        dot += av * bv
        na += av * av
        nb += bv * bv
    if na <= 0.0 or nb <= 0.0:
        return 1.0
    sim = dot / (math.sqrt(na) * math.sqrt(nb))
    if sim > 1.0:
        sim = 1.0
    if sim < -1.0:
        sim = -1.0
    return 1.0 - sim


class _InMemoryCollection:
    def __init__(self, *, name: str, metadata: dict[str, Any] | None = None):
        self._name = str(name)
        self._metadata = dict(metadata or {})
        self._lock = threading.RLock()
        self._docs: dict[str, str] = {}
        self._metas: dict[str, dict[str, Any]] = {}
        self._embs: dict[str, list[float]] = {}

    def upsert(
        self,
        *,
        ids: list[str],
        documents: list[str] | None = None,
        metadatas: list[dict[str, Any]] | None = None,
        embeddings: list[list[float]] | None = None,
    ) -> None:
        documents = documents or []
        metadatas = metadatas or []
        embeddings = embeddings or []
        with self._lock:
            for idx, raw_id in enumerate(ids or []):
                doc = documents[idx] if idx < len(documents) else ""
                meta = metadatas[idx] if idx < len(metadatas) and isinstance(metadatas[idx], dict) else {}
                emb = embeddings[idx] if idx < len(embeddings) else []
                rid = str(raw_id)
                self._docs[rid] = str(doc or "")
                self._metas[rid] = dict(meta)
                self._embs[rid] = [float(x) for x in (emb or [])]

    def query(
        self,
        *,
        query_embeddings: list[list[float]],
        n_results: int,
        where: dict[str, Any] | None = None,
        include: list[str] | None = None,
    ) -> dict[str, Any]:
        q = query_embeddings[0] if query_embeddings else []
        where = where or {}

        def _meta_match(meta: dict[str, Any]) -> bool:
            for key, value in where.items():
                if str(meta.get(key)) != str(value):
                    return False
            return True

        with self._lock:
            scored: list[tuple[float, str]] = []
            for rid, emb in self._embs.items():
                meta = self._metas.get(rid) or {}
                if where and not _meta_match(meta):
                    continue
                scored.append((_cosine_distance(q, emb), rid))

            scored.sort(key=lambda item: item[0])
            top = scored[: max(0, int(n_results))]
            ids = [rid for _, rid in top]
            return {
                "ids": [ids],
                "documents": [[self._docs.get(rid, "") for rid in ids]],
                "metadatas": [[dict(self._metas.get(rid, {})) for rid in ids]],
                "distances": [[float(distance) for distance, _ in top]],
            }

    def get(
        self,
        *,
        include: list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            ids = list(self._docs.keys())
            off = max(0, int(offset or 0))
            lim = int(limit) if limit is not None else None
            sliced = ids[off : off + lim] if lim is not None else ids[off:]

            out: dict[str, Any] = {"ids": sliced}
            requested = set(include or [])
            if not include or "documents" in requested:
                out["documents"] = [self._docs.get(rid, "") for rid in sliced]
            if not include or "metadatas" in requested:
                out["metadatas"] = [dict(self._metas.get(rid, {})) for rid in sliced]
            if not include or "embeddings" in requested:
                out["embeddings"] = [list(self._embs.get(rid, [])) for rid in sliced]
            return out


_INMEMORY_CHROMA: dict[str, dict[str, _InMemoryCollection]] = {}
_INMEMORY_CHROMA_LOCK = threading.RLock()


class _InMemoryClient:
    def __init__(self, *, path: str):
        self._path = str(path or "inmemory")
        with _INMEMORY_CHROMA_LOCK:
            _INMEMORY_CHROMA.setdefault(self._path, {})

    def get_or_create_collection(self, *, name: str, metadata: dict[str, Any] | None = None) -> _InMemoryCollection:
        with _INMEMORY_CHROMA_LOCK:
            store = _INMEMORY_CHROMA.setdefault(self._path, {})
            key = str(name)
            collection = store.get(key)
            if collection is None:
                collection = _InMemoryCollection(name=key, metadata=metadata)
                store[key] = collection
            return collection

    def get_collection(self, *, name: str) -> _InMemoryCollection:
        with _INMEMORY_CHROMA_LOCK:
            store = _INMEMORY_CHROMA.get(self._path) or {}
            key = str(name)
            collection = store.get(key)
            if collection is None:
                raise ValueError("collection does not exist")
            return collection

    def delete_collection(self, *, name: str) -> None:
        with _INMEMORY_CHROMA_LOCK:
            store = _INMEMORY_CHROMA.get(self._path) or {}
            key = str(name)
            if key not in store:
                raise ValueError("collection does not exist")
            del store[key]


class _InMemoryChromaModule:
    PersistentClient = _InMemoryClient


_INMEMORY_CHROMADB = _InMemoryChromaModule()
