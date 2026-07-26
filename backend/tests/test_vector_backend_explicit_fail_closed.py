"""backend-data#4 残留：显式 VECTOR_BACKEND=pgvector 但 pgvector 不可用时必须硬失败。

终审语义（deploy#1 OR 契约）：auto/chroma 仍可回落 Chroma；只有运维显式选择
pgvector 且探活失败时禁止静默换后端——ingest/query/purge 一律抛
PGVECTOR_BACKEND_UNAVAILABLE，绝不触碰 Chroma。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.core.config import settings
from app.core.errors import AppError
from app.services import vector_retrieval, vector_storage
from app.services.vector_types import VectorChunk


def _chunk() -> VectorChunk:
    return VectorChunk(
        id="c1",
        text="hello",
        metadata={"source": "chapter", "source_id": "s1", "chunk_index": 0},
    )


def _assert_backend_unavailable(exc: AppError) -> None:
    assert exc.code == "PGVECTOR_BACKEND_UNAVAILABLE"
    assert exc.status_code == 503
    assert exc.details.get("configured_backend") == "pgvector"


def test_explicit_pgvector_not_ready_fails_ingest_without_chroma_fallback(monkeypatch) -> None:
    monkeypatch.setattr(vector_storage, "_pgvector_ready", lambda: False)
    with (
        patch.object(settings, "vector_backend", "pgvector"),
        patch.object(vector_storage, "_get_collection") as chroma,
    ):
        with pytest.raises(AppError) as exc_info:
            vector_storage.ingest_chunks_with_embeddings(
                project_id="p1",
                chunks=[_chunk()],
                embeddings=[[0.1] * 1536],
            )
    _assert_backend_unavailable(exc_info.value)
    chroma.assert_not_called()


def test_explicit_pgvector_not_ready_fails_query_without_chroma_fallback(monkeypatch) -> None:
    monkeypatch.setattr(vector_storage, "_pgvector_ready", lambda: False)
    with (
        patch.object(settings, "vector_backend", "pgvector"),
        patch.object(vector_retrieval, "_vector_enabled_reason", return_value=(True, None)),
        patch.object(
            vector_retrieval,
            "embed_texts_with_providers",
            return_value={"enabled": True, "vectors": [[0.1] * 1536]},
        ),
        patch.object(vector_retrieval, "_get_collection") as chroma,
    ):
        with pytest.raises(AppError) as exc_info:
            vector_retrieval.query_project(project_id="p1", query_text="hello")
    _assert_backend_unavailable(exc_info.value)
    chroma.assert_not_called()


def test_explicit_pgvector_not_ready_fails_purges_without_chroma_fallback(monkeypatch) -> None:
    monkeypatch.setattr(vector_storage, "_pgvector_ready", lambda: False)
    with (
        patch.object(settings, "vector_backend", "pgvector"),
        patch.object(vector_storage, "_import_chromadb") as chroma,
    ):
        with pytest.raises(AppError) as project_exc:
            vector_storage.purge_project_vectors(project_id="p1")
        with pytest.raises(AppError) as document_exc:
            vector_storage.purge_document_vectors(project_id="p1", kb_id=None, document_id="d1")
    _assert_backend_unavailable(project_exc.value)
    _assert_backend_unavailable(document_exc.value)
    chroma.assert_not_called()


def test_auto_backend_still_falls_back_to_chroma_when_pgvector_not_ready(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(vector_storage, "_pgvector_ready", lambda: False)
    with (
        patch.object(settings, "vector_backend", "auto"),
        patch.object(settings, "vector_chroma_persist_dir", str(tmp_path)),
        patch.object(vector_storage, "_import_chromadb", return_value=vector_storage._INMEMORY_CHROMADB),
    ):
        out = vector_storage.ingest_chunks_with_embeddings(
            project_id="p1",
            chunks=[_chunk()],
            embeddings=[[0.1] * 8],
        )
    assert out["backend"] == "chroma"
    assert out["ingested"] == 1


def test_chroma_backend_never_probes_pgvector(monkeypatch, tmp_path) -> None:
    def _boom() -> bool:
        raise AssertionError("chroma backend must not probe pgvector readiness")

    monkeypatch.setattr(vector_storage, "_pgvector_ready", _boom)
    with (
        patch.object(settings, "vector_backend", "chroma"),
        patch.object(settings, "vector_chroma_persist_dir", str(tmp_path)),
        patch.object(vector_storage, "_import_chromadb", return_value=vector_storage._INMEMORY_CHROMADB),
    ):
        out = vector_storage.ingest_chunks_with_embeddings(
            project_id="p1",
            chunks=[_chunk()],
            embeddings=[[0.1] * 8],
        )
    assert out["backend"] == "chroma"
