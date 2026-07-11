from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

from app.api.routes import vector as vector_routes
from app.core.errors import AppError
from app.services import vector_retrieval, vector_storage
from app.services.vector_types import VectorChunk


_MIGRATION_FILE = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "a4c9d2e7f1b3_scope_pgvector_chunks_by_kb.py"
)
_SPEC = importlib.util.spec_from_file_location("_pgvector_kb_scope_migration", _MIGRATION_FILE)
assert _SPEC is not None and _SPEC.loader is not None
migration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migration)


class _Result:
    def __init__(self, value: int) -> None:
        self.value = value

    def scalar_one(self) -> int:
        return self.value


class _Bind:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, *, non_default: int = 0, duplicate_ids: int = 0) -> None:
        self.non_default = non_default
        self.duplicate_ids = duplicate_ids

    def execute(self, statement):  # type: ignore[no-untyped-def]
        return _Result(self.duplicate_ids if "HAVING COUNT(*) > 1" in str(statement) else self.non_default)


class _Op:
    def __init__(self, *, non_default: int = 0, duplicate_ids: int = 0) -> None:
        self.bind = _Bind(non_default=non_default, duplicate_ids=duplicate_ids)
        self.statements: list[str] = []

    def get_bind(self) -> _Bind:
        return self.bind

    def execute(self, statement) -> None:  # type: ignore[no-untyped-def]
        self.statements.append(str(statement))


def test_pgvector_kb_migration_builds_scoped_identity_and_indexes(monkeypatch) -> None:
    fake_op = _Op()
    monkeypatch.setattr(migration, "op", fake_op)

    migration.upgrade()

    sql = "\n".join(fake_op.statements)
    assert "ADD COLUMN kb_id VARCHAR(64) NOT NULL DEFAULT 'default'" in sql
    assert "PRIMARY KEY (project_id, kb_id, id)" in sql
    assert "vector_chunks(project_id, kb_id)" in sql
    assert "vector_chunks(project_id, kb_id, source)" in sql
    assert "ix_vector_chunks_project_id" in sql


def test_pgvector_kb_migration_downgrade_fails_closed(monkeypatch) -> None:
    fake_op = _Op(non_default=1)
    monkeypatch.setattr(migration, "op", fake_op)

    with pytest.raises(RuntimeError, match="non-default knowledge-base data"):
        migration.downgrade()

    assert fake_op.statements == []


def test_pgvector_kb_migration_downgrade_rejects_duplicate_ids(monkeypatch) -> None:
    fake_op = _Op(duplicate_ids=1)
    monkeypatch.setattr(migration, "op", fake_op)

    with pytest.raises(RuntimeError, match="duplicated across projects"):
        migration.downgrade()

    assert fake_op.statements == []


def test_pgvector_storage_sql_is_project_and_kb_scoped() -> None:
    source = inspect.getsource(vector_storage)
    assert "ON CONFLICT (project_id, kb_id, id)" in source
    assert "project_id = :project_id AND kb_id = :kb_id" in source
    assert "WHERE project_id = :project_id\n              AND kb_id = :kb_id" in source
    assert 'meta["kb_id"] = kb' in source
    retrieval_source = inspect.getsource(vector_retrieval.query_project)
    assert "for kid in selected_kb_ids:" in retrieval_source
    assert "kb_id=kid" in retrieval_source
    assert "sorted(selected_kb_ids" not in retrieval_source


def test_pgvector_rebuild_rolls_back_delete_when_upsert_fails(monkeypatch) -> None:
    db = MagicMock()
    monkeypatch.setattr(vector_storage, "SessionLocal", lambda: db)
    monkeypatch.setattr(vector_storage, "_prefer_pgvector", lambda: True)
    monkeypatch.setattr(vector_storage, "_vector_enabled_reason", lambda **_kwargs: (True, None))
    monkeypatch.setattr(
        vector_storage,
        "embed_texts_with_providers",
        lambda *_args, **_kwargs: {"enabled": True, "vectors": [[0.0] * 1536]},
    )
    monkeypatch.setattr(
        vector_storage,
        "_pgvector_upsert_chunks_in_session",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("write failed")),
    )

    with pytest.raises(AppError) as exc_info:
        vector_storage.rebuild_project(
            project_id="p1",
            kb_id="kb-a",
            chunks=[VectorChunk(id="c1", text="text", metadata={"source": "outline"})],
            embedding={
                "provider": "openai",
                "base_url": "https://embedding.invalid",
                "model": "test",
                "api_key": "test",
            },
        )

    assert exc_info.value.code == "PGVECTOR_REBUILD_FAILED"
    db.execute.assert_called_once()
    db.commit.assert_not_called()
    db.rollback.assert_called_once()
    db.close.assert_called_once()


def test_delete_kb_keeps_metadata_when_vector_purge_fails(monkeypatch) -> None:
    request = Request({"type": "http", "method": "DELETE", "path": "/", "headers": []})
    request.state.request_id = "rid"
    db = MagicMock()
    deleted = MagicMock()
    monkeypatch.setattr(vector_routes, "require_project_owner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(vector_routes, "get_vector_kb", lambda *_args, **_kwargs: SimpleNamespace(enabled=False))
    monkeypatch.setattr(
        vector_routes,
        "purge_project_vectors",
        lambda **_kwargs: {"deleted": False, "backend": "pgvector", "error_type": "OperationalError"},
    )
    monkeypatch.setattr(vector_routes, "delete_vector_kb", deleted)

    with pytest.raises(AppError) as exc_info:
        vector_routes.delete_vector_knowledge_base(
            request=request,
            db=db,
            user_id="u1",
            project_id="p1",
            kb_id="kb-a",
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.details == {
        "kb_id": "kb-a",
        "backend": "pgvector",
        "error_type": "OperationalError",
    }
    deleted.assert_not_called()
