from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


_VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"
_MIGRATION_FILE = _VERSIONS_DIR / "5da9e95bd9a3_repair_pgvector_vector_chunks.py"
# backend-data#4: 原迁移与修复迁移必须同为 fail-closed（全新库不得软跳过 pgvector）。
_ORIGINAL_MIGRATION_FILE = _VERSIONS_DIR / "4858b08a6519_add_pgvector_vector_chunks.py"
_MIGRATION_FILES = pytest.mark.parametrize(
    "migration_file",
    [_MIGRATION_FILE, _ORIGINAL_MIGRATION_FILE],
    ids=["repair", "original"],
)


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        return self._value


class _FakeBind:
    def __init__(self, *, dialect: str, extension_available: bool, fail_query: bool = False) -> None:
        self.dialect = SimpleNamespace(name=dialect)
        self.extension_available = extension_available
        self.fail_query = fail_query
        self.queries: list[str] = []

    def execute(self, statement: object) -> _ScalarResult:
        self.queries.append(str(statement))
        if self.fail_query:
            raise ConnectionError("extension catalog unavailable")
        return _ScalarResult(1 if self.extension_available else None)


class _FakeOp:
    def __init__(self, bind: _FakeBind, *, fail_create_extension: bool = False) -> None:
        self.bind = bind
        self.fail_create_extension = fail_create_extension
        self.statements: list[str] = []

    def get_bind(self) -> _FakeBind:
        return self.bind

    def execute(self, statement: object) -> None:
        sql = str(statement)
        self.statements.append(sql)
        if self.fail_create_extension and sql == "CREATE EXTENSION IF NOT EXISTS vector":
            raise PermissionError("extension install denied")


def _load_migration(migration_file: Path = _MIGRATION_FILE) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_pgvector_migration_{migration_file.stem}", migration_file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(
    *,
    dialect: str,
    extension_available: bool,
    fail_query: bool = False,
    fail_create_extension: bool = False,
    migration_file: Path = _MIGRATION_FILE,
) -> _FakeOp:
    module = _load_migration(migration_file)
    fake_op = _FakeOp(
        _FakeBind(dialect=dialect, extension_available=extension_available, fail_query=fail_query),
        fail_create_extension=fail_create_extension,
    )
    module.op = fake_op
    upgrade = module.upgrade
    assert callable(upgrade)
    upgrade()
    return fake_op


@_MIGRATION_FILES
def test_migration_creates_extension_table_and_all_indexes(migration_file: Path) -> None:
    fake_op = _run_upgrade(dialect="postgresql", extension_available=True, migration_file=migration_file)

    assert fake_op.statements[0] == "CREATE EXTENSION IF NOT EXISTS vector"
    assert "CREATE TABLE IF NOT EXISTS vector_chunks" in fake_op.statements[1]
    assert "embedding vector(1536) NOT NULL" in fake_op.statements[1]

    ddl = "\n".join(fake_op.statements)
    for index_name in (
        "ix_vector_chunks_project_id",
        "ix_vector_chunks_project_id_source",
        "ix_vector_chunks_content_tsv",
        "ix_vector_chunks_embedding_ivfflat",
    ):
        assert f"CREATE INDEX IF NOT EXISTS {index_name}" in ddl


@_MIGRATION_FILES
def test_migration_skips_non_postgres_databases(migration_file: Path) -> None:
    fake_op = _run_upgrade(dialect="sqlite", extension_available=True, migration_file=migration_file)

    assert fake_op.bind.queries == []
    assert fake_op.statements == []


@_MIGRATION_FILES
def test_migration_rejects_postgres_without_pgvector_package(migration_file: Path) -> None:
    with pytest.raises(RuntimeError, match="pgvector extension is unavailable"):
        _run_upgrade(dialect="postgresql", extension_available=False, migration_file=migration_file)


@_MIGRATION_FILES
def test_migration_propagates_extension_catalog_errors(migration_file: Path) -> None:
    with pytest.raises(ConnectionError, match="extension catalog unavailable"):
        _run_upgrade(
            dialect="postgresql",
            extension_available=False,
            fail_query=True,
            migration_file=migration_file,
        )


@_MIGRATION_FILES
def test_migration_propagates_extension_install_denial(migration_file: Path) -> None:
    with pytest.raises(PermissionError, match="extension install denied"):
        _run_upgrade(
            dialect="postgresql",
            extension_available=True,
            fail_create_extension=True,
            migration_file=migration_file,
        )


def test_repair_migration_downgrade_preserves_existing_vector_data() -> None:
    module = _load_migration()
    fake_op = _FakeOp(_FakeBind(dialect="postgresql", extension_available=True))
    module.op = fake_op
    downgrade = module.downgrade
    assert callable(downgrade)

    downgrade()

    assert fake_op.statements == []
