from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from app.db import migrations


PREVIOUS_REVISION = "9f3a7c2d1e4b"
CLEANUP_REVISION = "b2d4e6f8a0c1"
HEAD_REVISION = "d1f6a9b3c8e2"
_ARCHIVE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "archive_retired_tables.py"


def _load_archive_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_archive_retired_tables_for_migration", _ARCHIVE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_alembic(database_url: str, revision: str, *, downgrade: bool = False) -> None:
    config = migrations._alembic_config(database_url=database_url)
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        if downgrade:
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def _database_revision(engine: sa.Engine) -> str:
    with engine.connect() as connection:
        return str(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one())


def _normalized_sql(value: object) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).replace('"', "").split())


def _schema_snapshot(engine: sa.Engine, table_names: tuple[str, ...]) -> dict[str, Any]:
    inspector = sa.inspect(engine)
    snapshot: dict[str, Any] = {}
    for table_name in table_names:
        snapshot[table_name] = {
            "columns": [
                (
                    column["name"],
                    str(column["type"]),
                    bool(column["nullable"]),
                    _normalized_sql(column.get("default")),
                    int(column.get("primary_key") or 0),
                )
                for column in inspector.get_columns(table_name)
            ],
            "pk": inspector.get_pk_constraint(table_name),
            "unique": sorted(
                (
                    constraint.get("name"),
                    tuple(constraint.get("column_names") or ()),
                )
                for constraint in inspector.get_unique_constraints(table_name)
            ),
            "checks": sorted(
                (
                    constraint.get("name"),
                    _normalized_sql(constraint.get("sqltext")),
                )
                for constraint in inspector.get_check_constraints(table_name)
            ),
            "foreign_keys": sorted(
                (
                    tuple(constraint.get("constrained_columns") or ()),
                    constraint.get("referred_table"),
                    tuple(constraint.get("referred_columns") or ()),
                    tuple(sorted((constraint.get("options") or {}).items())),
                )
                for constraint in inspector.get_foreign_keys(table_name)
            ),
            "indexes": sorted(
                (
                    index.get("name"),
                    tuple(index.get("column_names") or ()),
                    bool(index.get("unique")),
                )
                for index in inspector.get_indexes(table_name)
            ),
        }
    return snapshot


def _seed_retired_entity(engine: sa.Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            INSERT INTO entities (
                id, project_id, entity_type, name, created_at, updated_at
            ) VALUES (
                'legacy-entity', 'legacy-project', 'person', 'Legacy',
                '2026-07-11 00:00:00+00:00', '2026-07-11 00:00:00+00:00'
            )
            """
        )


def test_cleanup_refuses_nonempty_tables_before_any_drop(tmp_path: Path) -> None:
    archive_module = _load_archive_module()
    database_url = f"sqlite:///{(tmp_path / 'nonempty.db').as_posix()}"
    _run_alembic(database_url, PREVIOUS_REVISION)
    engine = sa.create_engine(database_url)
    try:
        _seed_retired_entity(engine)
        with pytest.raises(RuntimeError, match=r"entities=1.*archive_retired_tables\.py"):
            _run_alembic(database_url, CLEANUP_REVISION)

        assert set(archive_module.RETIRED_TABLES).issubset(sa.inspect(engine).get_table_names())
        assert _database_revision(engine) == PREVIOUS_REVISION
    finally:
        engine.dispose()


def test_verified_archive_purge_allows_cleanup_upgrade(tmp_path: Path) -> None:
    archive_module = _load_archive_module()
    database_url = f"sqlite:///{(tmp_path / 'archived.db').as_posix()}"
    archive_dir = tmp_path / "retired-archive"
    _run_alembic(database_url, PREVIOUS_REVISION)
    engine = sa.create_engine(database_url)
    try:
        _seed_retired_entity(engine)
        archived = archive_module.archive(engine, archive_dir)
        purged = archive_module.purge(
            engine,
            archive_dir,
            confirmation=archive_module.PURGE_CONFIRMATION,
        )
        assert archived["total_rows"] == 1
        assert purged["total_deleted_rows"] == 1

        _run_alembic(database_url, CLEANUP_REVISION)
        table_names = set(sa.inspect(engine).get_table_names())
        assert table_names.isdisjoint(archive_module.RETIRED_TABLES)
        assert {"users", "projects", "story_memories"}.issubset(table_names)
        assert _database_revision(engine) == CLEANUP_REVISION
    finally:
        engine.dispose()


def test_cleanup_downgrade_recreates_exact_schema_and_reupgrades_to_head(tmp_path: Path) -> None:
    archive_module = _load_archive_module()
    database_url = f"sqlite:///{(tmp_path / 'round-trip.db').as_posix()}"
    _run_alembic(database_url, PREVIOUS_REVISION)
    engine = sa.create_engine(database_url)
    try:
        expected = _schema_snapshot(engine, archive_module.RETIRED_TABLES)

        _run_alembic(database_url, CLEANUP_REVISION)
        assert set(sa.inspect(engine).get_table_names()).isdisjoint(archive_module.RETIRED_TABLES)

        _run_alembic(database_url, PREVIOUS_REVISION, downgrade=True)
        assert _schema_snapshot(engine, archive_module.RETIRED_TABLES) == expected

        _run_alembic(database_url, "head")
        config = migrations._alembic_config(database_url=database_url)
        assert ScriptDirectory.from_config(config).get_heads() == [HEAD_REVISION]
        assert _database_revision(engine) == HEAD_REVISION
        assert set(sa.inspect(engine).get_table_names()).isdisjoint(archive_module.RETIRED_TABLES)
    finally:
        engine.dispose()
