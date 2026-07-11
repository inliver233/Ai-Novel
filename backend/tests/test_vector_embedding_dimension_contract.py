from __future__ import annotations

from unittest.mock import patch
from pathlib import Path

import pytest

from app.core.errors import AppError
from app.core.config import settings
from app.services import embedding_service, vector_retrieval, vector_storage
from app.services.embedding_contract import effective_vector_backend, validate_embedding_expected_dimension
from app.services.vector_embedding_overrides import embedding_config_fingerprint, vector_embedding_overrides
from app.models.project_settings import ProjectSettings
from app.schemas.vector_rag_profiles import VectorRagProfileCreate, VectorRagProfileOut
from datetime import datetime, timezone
from app.services.vector_types import VectorChunk


def test_embedding_batch_enforces_expected_dimension_and_uniformity() -> None:
    embedding = {
        "provider": "custom",
        "base_url": "https://embedding.invalid",
        "model": "test",
        "api_key": "secret",
        "expected_dimension": 3,
    }
    with patch.object(embedding_service, "_embed_openai_compatible", return_value=[[1.0, 2.0, 3.0]]):
        result = embedding_service.embed_texts(["text"], embedding=embedding)
    assert result["dimension"] == 3

    with (
        patch.object(embedding_service, "_embed_openai_compatible", return_value=[[1.0, 2.0]]),
        pytest.raises(AppError) as exc_info,
    ):
        embedding_service.embed_texts(["text"], embedding=embedding)
    assert exc_info.value.code == "EMBEDDING_DIMENSION_MISMATCH"

    with pytest.raises(AppError) as empty:
        embedding_service.embed_texts([], embedding=embedding)
    assert empty.value.code == "EMBEDDING_BATCH_EMPTY"


def test_pgvector_rejects_non_1536_write_without_chroma_fallback() -> None:
    chunk = VectorChunk(id="chunk", text="text", metadata={"source": "chapter"})
    with (
        patch.object(vector_storage, "_prefer_pgvector", return_value=True),
        patch.object(vector_storage, "_get_collection") as chroma,
        pytest.raises(AppError) as exc_info,
    ):
        vector_storage.ingest_chunks_with_embeddings(
            project_id="project",
            kb_id="default",
            chunks=[chunk],
            embeddings=[[1.0, 2.0]],
        )
    assert exc_info.value.code == "PGVECTOR_DIMENSION_UNSUPPORTED"
    chroma.assert_not_called()


def test_pgvector_sql_failure_does_not_fallback_to_chroma() -> None:
    chunk = VectorChunk(id="chunk", text="text", metadata={"source": "chapter"})
    with (
        patch.object(vector_storage, "_prefer_pgvector", return_value=True),
        patch.object(vector_storage, "_pgvector_upsert_chunks", side_effect=RuntimeError("sql failed")),
        patch.object(vector_storage, "_get_collection") as chroma,
        pytest.raises(AppError) as exc_info,
    ):
        vector_storage.ingest_chunks_with_embeddings(
            project_id="project",
            kb_id="default",
            chunks=[chunk],
            embeddings=[[0.0] * 1536],
        )
    assert exc_info.value.code == "PGVECTOR_WRITE_FAILED"
    chroma.assert_not_called()


def test_pgvector_query_dimension_mismatch_is_fail_closed() -> None:
    with (
        patch.object(vector_retrieval, "_vector_enabled_reason", return_value=(True, None)),
        patch.object(
            vector_retrieval,
            "embed_texts_with_providers",
            return_value={"enabled": True, "vectors": [[1.0, 2.0]]},
        ),
        patch.object(vector_retrieval, "_prefer_pgvector", return_value=True),
        patch.object(vector_retrieval, "_get_collection") as chroma,
        pytest.raises(AppError) as exc_info,
    ):
        vector_retrieval.query_project(
            project_id="project",
            kb_ids=["default"],
            query_text="query",
            sources=["chapter"],
            embedding={"expected_dimension": 2},
        )
    assert exc_info.value.code == "PGVECTOR_DIMENSION_UNSUPPORTED"
    chroma.assert_not_called()


def test_pgvector_cjk_lexical_sql_keeps_scope_and_trigram_fallback() -> None:
    executed: list[tuple[str, dict]] = []

    class _Result:
        def all(self):
            return []

    class _Session:
        def execute(self, statement, params):  # type: ignore[no-untyped-def]
            executed.append((str(statement), dict(params)))
            return _Result()

        def close(self):
            return None

    with patch.object(vector_storage, "SessionLocal", return_value=_Session()):
        vector_storage._pgvector_hybrid_fetch(
            project_id="project",
            kb_id="kb-a",
            query_text="龙王_长安%",
            query_vec=[0.0] * 1536,
            sources=["chapter"],
            vector_k=0,
            fts_k=10,
            rrf_k=60,
        )

    lexical_sql, params = executed[1]
    assert "text_md ILIKE :qpattern" in lexical_sql
    assert "text_md %> :qtext" in lexical_sql
    assert "word_similarity" in lexical_sql
    assert "project_id = :project_id AND kb_id = :kb_id" in lexical_sql
    assert params["qpattern"] == r"%龙王\_长安\%%"


def test_dimension_trigram_migration_contract() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "d1f6a9b3c8e2_add_embedding_dimension_and_trigram.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision = "c7e2f9a4b6d8"' in migration
    assert "vector_embedding_expected_dimension" in migration
    assert "vector_rag_profiles" in migration
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in migration
    assert "USING GIN (text_md gin_trgm_ops)" in migration
    assert "DROP EXTENSION" not in migration


def test_chroma_dimension_switch_requires_authoritative_rebuild(tmp_path) -> None:  # type: ignore[no-untyped-def]
    chunk = VectorChunk(id="chunk", text="text", metadata={"source": "chapter", "source_id": "chunk"})
    with (
        patch.object(settings, "vector_backend", "chroma"),
        patch.object(settings, "vector_chroma_persist_dir", str(tmp_path / "chroma-dimension")),
        patch.object(vector_storage, "_import_chromadb", return_value=vector_storage._INMEMORY_CHROMADB),
    ):
        vector_storage.ingest_chunks_with_embeddings(
            project_id="project",
            kb_id="default",
            chunks=[chunk],
            embeddings=[[1.0, 2.0]],
        )
        with pytest.raises(AppError) as mismatch:
            vector_storage.ingest_chunks_with_embeddings(
                project_id="project",
                kb_id="default",
                chunks=[chunk],
                embeddings=[[1.0, 2.0, 3.0]],
            )
        assert mismatch.value.code == "CHROMA_DIMENSION_MISMATCH"

        rebuilt = vector_storage.rebuild_project_with_embeddings(
            project_id="project",
            kb_id="default",
            chunks=[chunk],
            embeddings=[[1.0, 2.0, 3.0]],
        )
        assert rebuilt["rebuilt"] == 1


def test_expected_dimension_uses_effective_backend_contract() -> None:
    assert effective_vector_backend(configured_backend="chroma", dialect_name="postgresql") == "chroma"
    assert validate_embedding_expected_dimension(768, configured_backend="chroma", dialect_name="postgresql") == 768
    assert validate_embedding_expected_dimension(768, configured_backend="auto", dialect_name="sqlite") == 768
    for backend, dialect in (("auto", "postgresql"), ("pgvector", "sqlite")):
        with pytest.raises(AppError) as exc_info:
            validate_embedding_expected_dimension(768, configured_backend=backend, dialect_name=dialect)
        assert exc_info.value.code == "PGVECTOR_DIMENSION_UNSUPPORTED"


def test_vector_rag_profile_dimension_schema_defaults_and_roundtrips() -> None:
    assert VectorRagProfileCreate(name="Default").vector_embedding_expected_dimension == 1536
    profile = VectorRagProfileOut(
        id="profile",
        owner_user_id="user",
        name="Custom",
        vector_embedding_expected_dimension=768,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    assert profile.vector_embedding_expected_dimension == 768
