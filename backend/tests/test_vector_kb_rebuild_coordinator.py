from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.base import Base
from app.models.chapter import Chapter
from app.models.knowledge_base import KnowledgeBase
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_source_document import ProjectSourceDocument, ProjectSourceDocumentChunk
from app.models.project_settings import ProjectSettings
from app.models.story_memory import StoryMemory
from app.models.user import User
from app.services import import_export_service, vector_rebuild_coordinator, vector_storage
from app.services.vector_chunk_builder import build_kb_chunk_plan
from app.services.vector_types import VectorChunk


PROJECT_ID = "kb-plan-project"
USER_ID = "kb-plan-user"


@pytest.fixture
def session_factory(tmp_path) -> sessionmaker[Session]:  # type: ignore[no-untyped-def]
    engine = create_engine(
        f"sqlite:///{tmp_path / 'kb-plan.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            Project.__table__,
            Outline.__table__,
            Chapter.__table__,
            StoryMemory.__table__,
            KnowledgeBase.__table__,
            ProjectSettings.__table__,
            ProjectSourceDocument.__table__,
            ProjectSourceDocumentChunk.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add(User(id=USER_ID, display_name="KB owner"))
        db.add(Project(id=PROJECT_ID, owner_user_id=USER_ID, name="KB plan"))
        db.add(ProjectSettings(project_id=PROJECT_ID))
        db.add_all(
            [
                KnowledgeBase(id="kb-default", project_id=PROJECT_ID, kb_id="default", name="Default"),
                KnowledgeBase(id="kb-a-row", project_id=PROJECT_ID, kb_id="kb-a", name="A"),
                KnowledgeBase(id="kb-b-row", project_id=PROJECT_ID, kb_id="kb-b", name="B"),
                KnowledgeBase(id="kb-empty-row", project_id=PROJECT_ID, kb_id="kb-empty", name="Empty"),
            ]
        )
        db.add(Outline(id="outline-1", project_id=PROJECT_ID, title="Native outline", content_md="outline body"))
        db.add(
            Chapter(
                id="chapter-1",
                project_id=PROJECT_ID,
                outline_id="outline-1",
                number=1,
                title="Native chapter",
                content_md="chapter body",
            )
        )
        db.add(
            StoryMemory(
                id="memory-1",
                project_id=PROJECT_ID,
                memory_type="fact",
                title="Native memory",
                content="memory body",
            )
        )
        db.add_all(
            [
                ProjectSourceDocument(
                    id="doc-null",
                    project_id=PROJECT_ID,
                    actor_user_id=USER_ID,
                    filename="legacy.txt",
                    content_type="txt",
                    content_text="legacy default document",
                    status="done",
                    kb_id=None,
                ),
                ProjectSourceDocument(
                    id="doc-a",
                    project_id=PROJECT_ID,
                    actor_user_id=USER_ID,
                    filename="a.txt",
                    content_type="txt",
                    content_text="fallback must not replace persisted",
                    status="done",
                    kb_id="kb-a",
                ),
                ProjectSourceDocument(
                    id="doc-b",
                    project_id=PROJECT_ID,
                    actor_user_id=USER_ID,
                    filename="b.txt",
                    content_type="txt",
                    content_text="shared duplicate text",
                    status="done",
                    kb_id="kb-b",
                ),
                ProjectSourceDocument(
                    id="doc-queued",
                    project_id=PROJECT_ID,
                    actor_user_id=USER_ID,
                    filename="queued.txt",
                    content_type="txt",
                    content_text="must stay absent",
                    status="queued",
                    kb_id="kb-a",
                ),
            ]
        )
        db.add(
            ProjectSourceDocumentChunk(
                id="doc-a-chunk",
                document_id="doc-a",
                chunk_index=0,
                content_text="shared duplicate text",
                vector_chunk_id="persisted-doc-a-vector",
            )
        )
        db.commit()
    try:
        yield factory
    finally:
        engine.dispose()


def test_kb_plan_enforces_native_and_document_ownership(session_factory) -> None:  # type: ignore[no-untyped-def]
    with session_factory() as db:
        plan = build_kb_chunk_plan(
            db=db,
            project_id=PROJECT_ID,
            kb_ids=["default", "kb-a", "kb-b", "kb-empty"],
        )

    default_origins = {chunk.metadata.get("source_origin") for chunk in plan["default"]}
    assert None in default_origins
    assert "project_source_document" in default_origins
    assert {
        chunk.metadata.get("source_document_id") for chunk in plan["default"] if chunk.metadata.get("source_origin")
    } == {"doc-null"}

    assert [chunk.id for chunk in plan["kb-a"]] == ["persisted-doc-a-vector"]
    assert [chunk.metadata["source_document_id"] for chunk in plan["kb-a"]] == ["doc-a"]
    assert all(chunk.metadata["source_origin"] == "project_source_document" for chunk in plan["kb-a"])
    assert [chunk.id for chunk in plan["kb-b"]] == ["source_doc:doc-b:0"]
    assert plan["kb-b"][0].metadata["knowledge_base_id"] == "kb-b"
    assert plan["kb-empty"] == []
    assert "doc-queued" not in {
        chunk.metadata.get("source_document_id") for chunks in plan.values() for chunk in chunks
    }

    with session_factory() as db:
        outline_only = build_kb_chunk_plan(
            db=db,
            project_id=PROJECT_ID,
            kb_ids=["default", "kb-a"],
            sources=["outline"],
        )
    assert outline_only["default"]
    assert {chunk.metadata["source"] for chunk in outline_only["default"]} == {"outline"}
    assert outline_only["kb-a"] == []


def test_bulk_rebuild_embeds_unique_text_once_and_clears_empty_kb(session_factory) -> None:  # type: ignore[no-untyped-def]
    embedding_calls: list[list[str]] = []
    writes: dict[str, tuple[list[VectorChunk], list[list[float]]]] = {}

    def _embed(texts, *, embedding):  # type: ignore[no-untyped-def]
        embedding_calls.append(list(texts))
        return {"enabled": True, "vectors": [[float(index + 1)] for index, _ in enumerate(texts)]}

    def _write(*, project_id, kb_id, chunks, embeddings):  # type: ignore[no-untyped-def]
        assert project_id == PROJECT_ID
        writes[str(kb_id)] = (list(chunks), list(embeddings))
        return {
            "enabled": True,
            "skipped": False,
            "rebuilt": len(chunks),
            "ingested": len(chunks),
            "backend": "test",
        }

    with (
        session_factory() as db,
        patch.object(vector_rebuild_coordinator, "_vector_enabled_reason", return_value=(True, None)),
        patch.object(vector_rebuild_coordinator, "embed_texts_with_providers", side_effect=_embed),
        patch.object(vector_rebuild_coordinator, "rebuild_project_with_embeddings", side_effect=_write),
    ):
        result = vector_rebuild_coordinator.rebuild_kb_vectors(
            db=db,
            project_id=PROJECT_ID,
            kb_ids=["kb-a", "kb-b", "kb-empty"],
            embedding={"provider": "test"},
        )

    assert len(embedding_calls) == 1
    assert embedding_calls[0] == ["shared duplicate text"]
    assert writes["kb-a"][1] == writes["kb-b"][1]
    assert writes["kb-empty"] == ([], [])
    assert result["embedding"] == {"calls": 1, "unique_texts": 1}
    assert result["kbs"]["selected"] == ["kb-a", "kb-b", "kb-empty"]


def test_import_retry_purges_only_the_document_scope(session_factory) -> None:  # type: ignore[no-untyped-def]
    with (
        patch.object(import_export_service, "SessionLocal", session_factory),
        patch.object(
            import_export_service,
            "purge_document_vectors",
            return_value={"enabled": True, "skipped": False, "deleted": 1},
        ) as purge,
    ):
        result = import_export_service.retry_import_task(project_id=PROJECT_ID, document_id="doc-a")

    assert result["ok"] is True
    purge.assert_called_once_with(project_id=PROJECT_ID, kb_id="kb-a", document_id="doc-a")
    with session_factory() as db:
        assert db.get(ProjectSourceDocument, "doc-a").status == "queued"  # type: ignore[union-attr]
        assert db.get(ProjectSourceDocumentChunk, "doc-a-chunk") is None
        assert db.get(ProjectSourceDocument, "doc-b").status == "done"  # type: ignore[union-attr]


def test_import_retry_preserves_sibling_vectors_in_shared_kb(session_factory, tmp_path) -> None:  # type: ignore[no-untyped-def]
    persist_dir = str(tmp_path / "retry-chroma")
    doc_a = VectorChunk(
        id="source_doc:doc-a:0",
        text="doc a old vector",
        metadata={"source": "chapter", "source_id": "doc-a", "chunk_index": 0},
    )
    doc_b = VectorChunk(
        id="source_doc:doc-b:0",
        text="doc b sibling vector",
        metadata={"source": "chapter", "source_id": "doc-b", "chunk_index": 0},
    )
    with (
        patch.object(settings, "vector_backend", "chroma"),
        patch.object(settings, "vector_chroma_persist_dir", persist_dir),
        patch.object(vector_storage, "_import_chromadb", return_value=vector_storage._INMEMORY_CHROMADB),
    ):
        vector_storage.ingest_chunks_with_embeddings(
            project_id=PROJECT_ID,
            kb_id="kb-a",
            chunks=[doc_a, doc_b],
            embeddings=[[1.0], [2.0]],
        )
        with patch.object(import_export_service, "SessionLocal", session_factory):
            import_export_service.retry_import_task(project_id=PROJECT_ID, document_id="doc-a")
        collection = vector_storage._get_collection(project_id=PROJECT_ID, kb_id="kb-a")
        snapshot = collection.get(include=["documents", "metadatas"])

    assert snapshot["ids"] == ["source_doc:doc-b:0"]
    assert snapshot["documents"] == ["doc b sibling vector"]


def test_chroma_runtime_replace_failure_restores_snapshot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    persist_dir = str(tmp_path / "rollback-chroma")
    old = VectorChunk(
        id="old",
        text="old durable text",
        metadata={"source": "chapter", "source_id": "old", "chunk_index": 0},
    )
    new = VectorChunk(
        id="new",
        text="new failed text",
        metadata={"source": "chapter", "source_id": "new", "chunk_index": 0},
    )
    with (
        patch.object(settings, "vector_backend", "chroma"),
        patch.object(settings, "vector_chroma_persist_dir", persist_dir),
        patch.object(vector_storage, "_import_chromadb", return_value=vector_storage._INMEMORY_CHROMADB),
    ):
        vector_storage.rebuild_project_with_embeddings(
            project_id="rollback-project",
            kb_id="custom",
            chunks=[old],
            embeddings=[[1.0]],
        )
        collection_type = type(vector_storage._get_collection(project_id="rollback-project", kb_id="custom"))
        original_upsert = collection_type.upsert
        calls = 0

        def _fail_once(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("injected upsert failure")
            return original_upsert(self, *args, **kwargs)

        with patch.object(collection_type, "upsert", _fail_once):
            result = vector_storage.rebuild_project_with_embeddings(
                project_id="rollback-project",
                kb_id="custom",
                chunks=[new],
                embeddings=[[2.0]],
            )
        fresh = vector_storage._get_collection(project_id="rollback-project", kb_id="custom")
        snapshot = fresh.get(include=["documents", "metadatas", "embeddings"])

    assert result["skipped"] is True
    assert result["restored"] is True
    assert snapshot["ids"] == ["old"]
    assert snapshot["documents"] == ["old durable text"]
    assert snapshot["embeddings"] == [[1.0]]


def test_empty_kb_is_cleared_without_embedding_configuration(session_factory) -> None:  # type: ignore[no-untyped-def]
    with (
        session_factory() as db,
        patch.object(
            vector_rebuild_coordinator,
            "rebuild_project_with_embeddings",
            return_value={"enabled": True, "skipped": False, "rebuilt": 0, "backend": "test"},
        ) as rebuild,
        patch.object(vector_rebuild_coordinator, "_vector_enabled_reason") as enabled_reason,
        patch.object(vector_rebuild_coordinator, "embed_texts_with_providers") as embed,
    ):
        result = vector_rebuild_coordinator.rebuild_kb_vectors(
            db=db,
            project_id=PROJECT_ID,
            kb_ids=["kb-empty"],
            embedding=None,
        )

    rebuild.assert_called_once_with(
        project_id=PROJECT_ID,
        kb_id="kb-empty",
        chunks=[],
        embeddings=[],
    )
    enabled_reason.assert_not_called()
    embed.assert_not_called()
    assert result["enabled"] is True
    assert result["skipped"] is False


def test_custom_ingest_replaces_authoritative_partition(session_factory) -> None:  # type: ignore[no-untyped-def]
    with (
        session_factory() as db,
        patch.object(vector_rebuild_coordinator, "_vector_enabled_reason", return_value=(True, None)),
        patch.object(
            vector_rebuild_coordinator,
            "embed_texts_with_providers",
            return_value={"enabled": True, "vectors": [[1.0]]},
        ),
        patch.object(
            vector_rebuild_coordinator,
            "rebuild_project_with_embeddings",
            return_value={"enabled": True, "skipped": False, "rebuilt": 1, "backend": "test"},
        ) as rebuild,
        patch.object(vector_rebuild_coordinator, "ingest_chunks_with_embeddings") as additive_ingest,
    ):
        result = vector_rebuild_coordinator.ingest_kb_vectors(
            db=db,
            project_id=PROJECT_ID,
            kb_ids=["kb-a"],
            embedding={"provider": "test"},
        )

    rebuild.assert_called_once()
    additive_ingest.assert_not_called()
    assert result["ingested"] == 1


def test_import_worker_rebuilds_the_owned_kb_with_shared_coordinator(session_factory) -> None:  # type: ignore[no-untyped-def]
    with session_factory() as db:
        doc = db.get(ProjectSourceDocument, "doc-a")
        assert doc is not None
        doc.status = "queued"
        doc.content_text = "fresh imported content"
        db.commit()

    with (
        patch.object(import_export_service, "SessionLocal", session_factory),
        patch.object(import_export_service, "vector_embedding_overrides", return_value={"provider": "test"}),
        patch.object(
            import_export_service,
            "rebuild_kb_vectors",
            return_value={"enabled": True, "skipped": False, "rebuilt": 1, "backend": "test"},
        ) as rebuild,
    ):
        import_export_service.run_import_task("doc-a")

    assert rebuild.call_count == 1
    call = rebuild.call_args.kwargs
    assert call["project_id"] == PROJECT_ID
    assert call["kb_ids"] == ["kb-a"]
    with session_factory() as db:
        doc = db.get(ProjectSourceDocument, "doc-a")
        assert doc is not None
        assert doc.status == "done"
        assert doc.progress == 100
        chunks = db.query(ProjectSourceDocumentChunk).filter_by(document_id="doc-a").all()
        assert [chunk.vector_chunk_id for chunk in chunks] == ["source_doc:doc-a:0"]


def test_real_chroma_rebuild_isolates_kbs_and_clears_empty_target(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import chromadb

    persist_dir = str(tmp_path / "chroma")

    def _embedding(first: float, second: float) -> list[float]:
        return [first, second, *([0.0] * 1534)]

    alpha_chunk = VectorChunk(
        id="alpha",
        text="alpha text",
        metadata={"source": "chapter", "source_id": "alpha", "chunk_index": 0},
    )
    beta_chunk = VectorChunk(
        id="beta",
        text="beta text",
        metadata={"source": "chapter", "source_id": "beta", "chunk_index": 0},
    )

    with (
        patch.object(settings, "vector_backend", "chroma"),
        patch.object(settings, "vector_chroma_persist_dir", persist_dir),
        patch.object(vector_storage, "_import_chromadb", return_value=chromadb),
    ):
        vector_storage.rebuild_project_with_embeddings(
            project_id="real-chroma-project",
            kb_id="alpha",
            chunks=[alpha_chunk],
            embeddings=[_embedding(1.0, 0.0)],
        )
        vector_storage.rebuild_project_with_embeddings(
            project_id="real-chroma-project",
            kb_id="beta",
            chunks=[beta_chunk],
            embeddings=[_embedding(0.0, 1.0)],
        )

        client = chromadb.PersistentClient(path=persist_dir)
        alpha = client.get_collection(name=vector_storage._hash_collection_name("real-chroma-project", "alpha"))
        collection_type = type(alpha)
        original_upsert = collection_type.upsert
        calls = 0

        def _fail_once(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("injected chroma upsert failure")
            return original_upsert(self, *args, **kwargs)

        with patch.object(collection_type, "upsert", _fail_once):
            failed = vector_storage.rebuild_project_with_embeddings(
                project_id="real-chroma-project",
                kb_id="alpha",
                chunks=[
                    VectorChunk(
                        id="alpha-new",
                        text="must not survive",
                        metadata={"source": "chapter", "source_id": "alpha-new", "chunk_index": 0},
                    )
                ],
                embeddings=[_embedding(0.5, 0.5)],
            )
        assert failed["skipped"] is True
        assert failed["restored"] is True
        fresh_client = chromadb.PersistentClient(path=persist_dir)
        restored_alpha = fresh_client.get_collection(
            name=vector_storage._hash_collection_name("real-chroma-project", "alpha")
        )
        assert restored_alpha.get(include=["documents"])["documents"] == ["alpha text"]

        vector_storage.rebuild_project_with_embeddings(
            project_id="real-chroma-project",
            kb_id="alpha",
            chunks=[],
            embeddings=[],
        )
        beta = client.get_collection(name=vector_storage._hash_collection_name("real-chroma-project", "beta"))
        assert beta.get(include=["documents"])["documents"] == ["beta text"]
        with pytest.raises(Exception):
            client.get_collection(name=vector_storage._hash_collection_name("real-chroma-project", "alpha"))


def test_call_sites_cannot_loop_over_legacy_per_kb_rebuild() -> None:
    root = Path(__file__).resolve().parents[1]
    guarded = [
        root / "app" / "api" / "routes" / "vector.py",
        root / "app" / "services" / "project_task_service.py",
        root / "app" / "services" / "import_export_service.py",
    ]
    for path in guarded:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert "rebuild_kb_vectors" in source or "ingest_kb_vectors" in source
        for loop in (node for node in ast.walk(tree) if isinstance(node, (ast.For, ast.AsyncFor))):
            calls = [node for node in ast.walk(loop) if isinstance(node, ast.Call)]
            assert all(
                not (
                    isinstance(call.func, ast.Name)
                    and call.func.id == "rebuild_project"
                    or isinstance(call.func, ast.Attribute)
                    and call.func.attr == "rebuild_project"
                )
                for call in calls
            )
