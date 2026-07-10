"""SQLite→Postgres 迁移脚本的完整表覆盖与依赖顺序测试。

deploy#2：旧脚本用 18 个硬编码表名驱动预检、复制和验证，遗漏的业务表
不会出现在报告中，最终形成静默数据丢失。修复后的工具必须从源/目标实际 schema
构建完整搬运计划，排除仅可重建的 SQLite FTS 表，并在任何源业务表无法落到目标
时于复制前失败。表顺序还必须满足所有外键，仅延后
``projects.active_outline_id -> outlines.id`` 这一条已由脚本显式清空/恢复的环边。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command

import app.models
from app.db import migrations
from app.db.base import Base

_MIGRATE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_sqlite_to_postgres.py"


def _load_migrate_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_migrate_sqlite_to_postgres", _MIGRATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _active_model_table_names() -> set[str]:
    tables: set[str] = set()
    for export_name in app.models.__all__:
        model = getattr(app.models, export_name)
        table = getattr(model, "__table__", None)
        if table is not None:
            tables.add(str(table.name))
    return tables


def _upgrade_to_head(database_url: str) -> None:
    cfg = migrations._alembic_config(database_url=database_url)
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(cfg, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


# deploy#2: 迁移计划必须覆盖所有当前活 ORM 表，且每张恰好一次。
def test_table_order_covers_all_active_tables() -> None:
    module = _load_migrate_module()
    active_tables = _active_model_table_names()
    ordered_tables = module._sorted_table_names(Base.metadata.tables[name] for name in active_tables)

    assert set(ordered_tables) == active_tables
    assert len(ordered_tables) == len(set(ordered_tables))


def test_table_order_respects_foreign_keys_except_deferred_project_outline_edge() -> None:
    module = _load_migrate_module()
    active_tables = _active_model_table_names()
    table_objects = [Base.metadata.tables[name] for name in active_tables]
    ordered_tables = module._sorted_table_names(table_objects)
    positions = {name: index for index, name in enumerate(ordered_tables)}

    assert positions["projects"] < positions["outlines"]
    for table in table_objects:
        for foreign_key in table.foreign_keys:
            referred_table = foreign_key.column.table.name
            if referred_table not in positions or module._is_deferred_project_outline_fk(foreign_key):
                continue
            assert positions[referred_table] < positions[table.name], (
                f"FK 顺序错误: {table.name}.{foreign_key.parent.name} -> {referred_table}"
            )


def test_copy_plan_covers_every_portable_head_table_and_skips_sqlite_fts(
    tmp_path: Path,
) -> None:
    module = _load_migrate_module()
    source_url = f"sqlite:///{(tmp_path / 'source.db').as_posix()}"
    target_url = f"sqlite:///{(tmp_path / 'target.db').as_posix()}"
    _upgrade_to_head(source_url)
    _upgrade_to_head(target_url)

    source_engine = sa.create_engine(source_url)
    target_engine = sa.create_engine(target_url)
    try:
        table_order, source_tables, target_tables, skipped_tables = module._build_copy_plan(
            src_engine=source_engine,
            dst_engine=target_engine,
        )
        source_table_names = set(sa.inspect(source_engine).get_table_names())
    finally:
        source_engine.dispose()
        target_engine.dispose()

    portable_source_tables = source_table_names - module._IGNORED_SOURCE_TABLES
    assert set(table_order) == portable_source_tables
    assert set(source_tables) == portable_source_tables
    assert set(target_tables) == portable_source_tables
    assert set(skipped_tables) == source_table_names & module._IGNORED_SOURCE_TABLES
    assert _active_model_table_names().issubset(table_order)
    assert {
        "entities",
        "relations",
        "memory_change_sets",
        "memory_change_set_items",
        "memory_tasks",
        "project_tables",
        "project_table_rows",
        "worldbook_entries",
        "plot_analysis",
    }.issubset(table_order)
    assert "search_index" not in table_order
    assert "vector_chunks" not in table_order


def test_copy_plan_rejects_source_application_table_missing_from_target() -> None:
    module = _load_migrate_module()
    source_engine = sa.create_engine("sqlite://")
    target_engine = sa.create_engine("sqlite://")
    try:
        with source_engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE users (id VARCHAR(64) PRIMARY KEY)"))
            conn.execute(sa.text("CREATE TABLE future_business_data (id VARCHAR(64) PRIMARY KEY)"))
        with target_engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE users (id VARCHAR(64) PRIMARY KEY)"))

        with pytest.raises(RuntimeError, match="future_business_data"):
            module._build_copy_plan(src_engine=source_engine, dst_engine=target_engine)
    finally:
        source_engine.dispose()
        target_engine.dispose()


def test_source_preflight_rejects_missing_or_unrecognized_sqlite_file(tmp_path: Path) -> None:
    module = _load_migrate_module()
    missing_path = tmp_path / "typo.db"
    missing_url = f"sqlite:///{missing_path.as_posix()}"

    with pytest.raises(SystemExit, match="does not exist"):
        module._require_existing_sqlite_source(missing_url)
    assert not missing_path.exists()

    empty_path = tmp_path / "empty.db"
    empty_path.touch()
    empty_engine = sa.create_engine(f"sqlite:///{empty_path.as_posix()}")
    try:
        with pytest.raises(RuntimeError, match="recognizable Ai-Novel schema"):
            module._validate_source_schema(empty_engine)
    finally:
        empty_engine.dispose()


def test_dry_run_reference_engine_provides_head_schema_without_target_database() -> None:
    module = _load_migrate_module()
    reference_engine = module._create_head_schema_reference_engine()
    try:
        table_names = set(sa.inspect(reference_engine).get_table_names())
    finally:
        reference_engine.dispose()

    assert {"users", "projects", "entries", "vector_rag_profiles"}.issubset(table_names)
    assert "alembic_version" in table_names
    assert "vector_chunks" not in table_names


def test_copy_table_preserves_rows_in_chunks_and_rolls_back_hook_failure() -> None:
    module = _load_migrate_module()
    source_engine = sa.create_engine("sqlite://")
    target_engine = sa.create_engine("sqlite://")
    source_metadata = sa.MetaData()
    target_metadata = sa.MetaData()
    source_table = sa.Table(
        "items",
        source_metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("value", sa.String(32), nullable=False),
    )
    target_table = sa.Table(
        "items",
        target_metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("value", sa.String(32), nullable=False),
    )
    source_metadata.create_all(source_engine)
    target_metadata.create_all(target_engine)

    try:
        with source_engine.begin() as conn:
            conn.execute(source_table.insert(), [{"id": index, "value": f"v{index}"} for index in range(1, 6)])
        with source_engine.connect() as source_conn:
            inserted = module._copy_table(
                src_conn=source_conn,
                dst_engine=target_engine,
                src_table=source_table,
                dst_table=target_table,
                chunk_size=2,
                resume=False,
            )
        with target_engine.connect() as conn:
            rows = conn.execute(sa.select(target_table).order_by(target_table.c.id)).all()
        assert inserted == 5
        assert rows == [(1, "v1"), (2, "v2"), (3, "v3"), (4, "v4"), (5, "v5")]

        with target_engine.begin() as conn:
            conn.execute(target_table.delete())

        def _fail_hook(_conn: sa.Connection) -> None:
            raise RuntimeError("restore failed")

        with source_engine.connect() as source_conn, pytest.raises(RuntimeError, match="restore failed"):
            module._copy_table(
                src_conn=source_conn,
                dst_engine=target_engine,
                src_table=source_table,
                dst_table=target_table,
                chunk_size=2,
                resume=False,
                post_insert_hook=_fail_hook,
            )
        with target_engine.connect() as conn:
            assert conn.execute(sa.select(sa.func.count()).select_from(target_table)).scalar_one() == 0
    finally:
        source_engine.dispose()
        target_engine.dispose()


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value

    def scalar_one(self) -> object:
        return self.value


class _FakePostgresConnection:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: object, params: dict[str, Any] | None = None) -> _ScalarResult:
        sql = str(statement)
        self.calls.append((sql, dict(params or {})))
        if "pg_get_serial_sequence" in sql:
            return _ScalarResult("public.search_documents_id_seq")
        if "max(" in sql.lower():
            return _ScalarResult(7)
        return _ScalarResult(None)


def test_reset_postgres_sequence_uses_maximum_preserved_id() -> None:
    module = _load_migrate_module()
    table = sa.Table(
        "search_documents",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    )
    connection = _FakePostgresConnection()

    module._reset_postgres_sequence(connection, table)

    assert len(connection.calls) == 3
    assert connection.calls[-1][1] == {
        "sequence_name": "public.search_documents_id_seq",
        "value": 7,
        "is_called": True,
    }
