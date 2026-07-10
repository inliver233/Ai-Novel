from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from scripts.check_alembic import (
    SchemaContractError,
    assert_schema_matches_metadata,
    check_empty_database,
    migration_head,
    schema_differences,
)


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _metadata(*, column: sa.Column[object] | None = None) -> sa.MetaData:
    metadata = sa.MetaData()
    columns: list[sa.Column[object]] = [sa.Column("id", sa.Integer(), primary_key=True)]
    if column is not None:
        columns.append(column)
    sa.Table("widgets", metadata, *columns)
    return metadata


def _diff_repr(engine: sa.Engine, metadata: sa.MetaData) -> str:
    with engine.connect() as connection:
        return repr(schema_differences(connection, metadata=metadata))


def test_empty_sqlite_upgrade_is_idempotent_current_and_drift_free(tmp_path: Path) -> None:
    report = check_empty_database(_sqlite_url(tmp_path / "contract.db"))

    assert report.head == migration_head(_sqlite_url(tmp_path / "unused.db"))
    assert report.current == report.head
    assert report.dialect == "sqlite"
    assert report.differences == ()


def test_contract_refuses_a_database_that_was_not_empty(tmp_path: Path) -> None:
    engine = sa.create_engine(_sqlite_url(tmp_path / "not-empty.db"))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE rogue (id INTEGER PRIMARY KEY)")
        with pytest.raises(SchemaContractError, match=r"requires an empty database.*tables=.*rogue"):
            check_empty_database(str(engine.url))
    finally:
        engine.dispose()


def test_contract_refuses_a_database_containing_only_a_view(tmp_path: Path) -> None:
    engine = sa.create_engine(_sqlite_url(tmp_path / "view.db"))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE VIEW rogue_view AS SELECT 1 AS value")
        with pytest.raises(SchemaContractError, match=r"user objects.*views=.*rogue_view"):
            check_empty_database(str(engine.url))
    finally:
        engine.dispose()


def test_autogenerate_detects_rogue_table(tmp_path: Path) -> None:
    engine = sa.create_engine(_sqlite_url(tmp_path / "rogue-table.db"))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE rogue (id INTEGER PRIMARY KEY)")
        differences = _diff_repr(engine, sa.MetaData())
        assert "remove_table" in differences
        assert "rogue" in differences
    finally:
        engine.dispose()


def test_autogenerate_detects_rogue_column(tmp_path: Path) -> None:
    engine = sa.create_engine(_sqlite_url(tmp_path / "rogue-column.db"))
    metadata = _metadata()
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE widgets (id INTEGER PRIMARY KEY, rogue TEXT)")
        differences = _diff_repr(engine, metadata)
        assert "remove_column" in differences
        assert "rogue" in differences
    finally:
        engine.dispose()


def test_autogenerate_detects_server_default_drift(tmp_path: Path) -> None:
    engine = sa.create_engine(_sqlite_url(tmp_path / "default.db"))
    metadata = _metadata(
        column=sa.Column("state", sa.String(20), nullable=False, server_default=sa.text("'ready'"))
    )
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE widgets (id INTEGER PRIMARY KEY, state VARCHAR(20) NOT NULL DEFAULT 'stale')"
            )
        assert "modify_default" in _diff_repr(engine, metadata)
    finally:
        engine.dispose()


def test_autogenerate_detects_nullable_drift(tmp_path: Path) -> None:
    engine = sa.create_engine(_sqlite_url(tmp_path / "nullable.db"))
    metadata = _metadata(column=sa.Column("name", sa.String(20), nullable=False))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name VARCHAR(20))")
        assert "modify_nullable" in _diff_repr(engine, metadata)
    finally:
        engine.dispose()


def test_autogenerate_detects_type_drift_and_public_assertion_fails(tmp_path: Path) -> None:
    engine = sa.create_engine(_sqlite_url(tmp_path / "type.db"))
    metadata = _metadata(column=sa.Column("quantity", sa.Integer(), nullable=True))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE widgets (id INTEGER PRIMARY KEY, quantity VARCHAR(20))")
        with engine.connect() as connection:
            assert "modify_type" in repr(schema_differences(connection, metadata=metadata))
            with pytest.raises(SchemaContractError, match="modify_type"):
                assert_schema_matches_metadata(connection, metadata=metadata)
    finally:
        engine.dispose()
