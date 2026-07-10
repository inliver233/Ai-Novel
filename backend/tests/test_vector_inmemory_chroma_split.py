"""Regression coverage for the shared process-local Chroma fallback."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from typing import Any

import pytest

from app.services import (
    vector_build,
    vector_chroma_fallback,
    vector_rag_service,
    vector_retrieval,
    vector_storage,
)


def _missing_chromadb() -> Any:
    raise ImportError("forced missing chromadb")


def _fake_embed(texts: list[str], **_kwargs: Any) -> dict[str, Any]:
    return {"enabled": True, "vectors": [[1.0, 0.0, 0.0] for _ in texts]}


def _enabled_ok(*_args: Any, **_kwargs: Any) -> tuple[bool, None]:
    return (True, None)


@pytest.fixture(autouse=True)
def _reset_shared_fallback() -> Any:
    vector_chroma_fallback._INMEMORY_CHROMA.clear()
    vector_chroma_fallback._CHROMADB_FALLBACK_WARNING_EMITTED.clear()
    yield
    vector_chroma_fallback._INMEMORY_CHROMA.clear()
    vector_chroma_fallback._CHROMADB_FALLBACK_WARNING_EMITTED.clear()


@pytest.fixture
def forced_inmemory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vector_chroma_fallback, "_load_chromadb", _missing_chromadb)
    monkeypatch.setattr(vector_storage, "embed_texts_with_providers", _fake_embed)
    monkeypatch.setattr(vector_retrieval, "embed_texts_with_providers", _fake_embed)
    monkeypatch.setattr(vector_build, "_vector_enabled_reason", _enabled_ok)
    monkeypatch.setattr(vector_rag_service, "_vector_enabled_reason", _enabled_ok)
    monkeypatch.setattr(vector_retrieval, "_vector_enabled_reason", _enabled_ok)
    monkeypatch.setattr(vector_storage, "_prefer_pgvector", lambda: False)
    monkeypatch.setattr(vector_retrieval, "_prefer_pgvector", lambda: False)


def _chunk(*, chunk_id: str, text: str) -> vector_rag_service.VectorChunk:
    return vector_rag_service.VectorChunk(
        id=chunk_id,
        text=text,
        metadata={"source": "chapter", "source_id": chunk_id, "chunk_index": 0},
    )


def test_inmemory_chroma_write_and_read_share_store(forced_inmemory: None) -> None:
    assert vector_build._INMEMORY_CHROMA is vector_storage._INMEMORY_CHROMA
    assert vector_build._INMEMORY_CHROMA is vector_chroma_fallback._INMEMORY_CHROMA

    chunk = _chunk(chunk_id="c1", text="hello world dragon")
    ingest_out = vector_rag_service.ingest_chunks(
        project_id="rag-4-2-shared",
        kb_id=None,
        chunks=[chunk],
    )

    assert ingest_out.get("enabled") is True, ingest_out
    assert ingest_out.get("ingested") == 1, ingest_out

    result = vector_rag_service.query_project(
        project_id="rag-4-2-shared",
        kb_id=None,
        query_text="hello world dragon",
        sources=["chapter"],
    )

    assert result["candidates"] == [
        {
            "id": "c1",
            "distance": 0.0,
            "text": "hello world dragon",
            "metadata": {
                "source": "chapter",
                "source_id": "c1",
                "chunk_index": 0,
                "kb_id": "default",
            },
        }
    ]


def test_inmemory_chroma_keeps_knowledge_bases_isolated(forced_inmemory: None) -> None:
    project_id = "rag-4-2-kb-isolation"
    for kb_id, chunk in (
        ("alpha", _chunk(chunk_id="alpha-c1", text="alpha dragon")),
        ("beta", _chunk(chunk_id="beta-c1", text="beta phoenix")),
    ):
        ingest_out = vector_rag_service.ingest_chunks(
            project_id=project_id,
            kb_id=kb_id,
            chunks=[chunk],
        )
        assert ingest_out.get("ingested") == 1, ingest_out

    alpha = vector_rag_service.query_project(
        project_id=project_id,
        kb_id="alpha",
        query_text="alpha dragon",
        sources=["chapter"],
    )
    beta = vector_rag_service.query_project(
        project_id=project_id,
        kb_id="beta",
        query_text="beta phoenix",
        sources=["chapter"],
    )

    assert [candidate["id"] for candidate in alpha["candidates"]] == ["alpha-c1"]
    assert [candidate["metadata"]["kb_id"] for candidate in alpha["candidates"]] == ["alpha"]
    assert [candidate["id"] for candidate in beta["candidates"]] == ["beta-c1"]
    assert [candidate["metadata"]["kb_id"] for candidate in beta["candidates"]] == ["beta"]


def test_inmemory_collection_serializes_query_and_upsert(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = vector_chroma_fallback._InMemoryCollection(name="concurrent")
    collection.upsert(ids=["c1"], documents=["one"], metadatas=[{}], embeddings=[[1.0]])

    distance_started = threading.Event()
    release_distance = threading.Event()
    upsert_attempted = threading.Event()
    original_cosine_distance = vector_chroma_fallback._cosine_distance

    def _blocking_cosine_distance(a: list[float], b: list[float]) -> float:
        distance_started.set()
        if not release_distance.wait(timeout=2):
            raise TimeoutError("query was not released")
        return original_cosine_distance(a, b)

    monkeypatch.setattr(vector_chroma_fallback, "_cosine_distance", _blocking_cosine_distance)

    def _concurrent_upsert() -> None:
        upsert_attempted.set()
        collection.upsert(
            ids=["c2"],
            documents=["two"],
            metadatas=[{}],
            embeddings=[[1.0]],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        query_future = executor.submit(
            collection.query,
            query_embeddings=[[1.0]],
            n_results=10,
        )
        try:
            assert distance_started.wait(timeout=1)
            upsert_future = executor.submit(_concurrent_upsert)
            assert upsert_attempted.wait(timeout=1)
            assert not upsert_future.done()
        finally:
            release_distance.set()

        result = query_future.result(timeout=1)
        upsert_future.result(timeout=1)

    assert result["ids"] == [["c1"]]
    assert collection.get()["ids"] == ["c1", "c2"]


def test_inmemory_client_creates_one_collection_under_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    original_collection = vector_chroma_fallback._InMemoryCollection

    class _SlowCollection(original_collection):
        def __init__(self, *, name: str, metadata: dict[str, Any] | None = None):
            time.sleep(0.02)
            super().__init__(name=name, metadata=metadata)

    monkeypatch.setattr(vector_chroma_fallback, "_InMemoryCollection", _SlowCollection)
    client = vector_chroma_fallback._InMemoryClient(path="concurrent-client")

    with ThreadPoolExecutor(max_workers=8) as executor:
        collections = list(
            executor.map(
                lambda _index: client.get_or_create_collection(name="shared"),
                range(8),
            )
        )

    assert all(collection is collections[0] for collection in collections)


def test_missing_chromadb_logs_shared_fallback_warning_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(vector_chroma_fallback, "_load_chromadb", _missing_chromadb)
    monkeypatch.setattr(
        vector_chroma_fallback,
        "log_event",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    first = vector_chroma_fallback.load_chromadb_or_fallback()
    second = vector_chroma_fallback.load_chromadb_or_fallback()

    assert first is vector_chroma_fallback._INMEMORY_CHROMADB
    assert second is first
    assert len(calls) == 1
    args, fields = calls[0]
    assert args == (vector_chroma_fallback.logger, "warning")
    assert fields["event"] == "VECTOR_RAG"
    assert fields["action"] == "chromadb_import_fallback"
    assert fields["backend"] == "chroma"
    assert fields["fallback_backend"] == "inmemory"
    assert fields["process_local"] is True
    assert fields["error_type"] == "ImportError"
