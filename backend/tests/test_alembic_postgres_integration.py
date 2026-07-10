from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.runtime.migration import MigrationContext

from app.db.migrations import _alembic_config
from scripts.check_alembic import (
    SchemaContractError,
    assert_schema_matches_metadata,
    check_empty_database,
    migration_head,
)


PRE_CLEANUP_REVISION = "9f3a7c2d1e4b"
HEAD_REVISION = "c7e2a4f6b8d0"
EXPECTED_DATABASE = "ainovel_schema_ci"
DESTRUCTIVE_SENTINEL = "I_UNDERSTAND_THIS_DROPS_PUBLIC_SCHEMA"
RETIRED_TABLES = {
    "entities",
    "relations",
    "events",
    "foreshadows",
    "evidence",
    "memory_change_sets",
    "memory_change_set_items",
    "memory_tasks",
    "project_tables",
    "project_table_rows",
    "worldbook_entries",
    "glossary_terms",
    "plot_analysis",
    "fractal_memory",
}


def _validate_destructive_url(database_url: str, *, sentinel: str | None) -> None:
    url = sa.engine.make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("TEST_POSTGRES_URL must be a PostgreSQL URL")
    if url.host not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("destructive PostgreSQL integration requires a loopback host")
    if url.database != EXPECTED_DATABASE:
        raise RuntimeError(f"destructive PostgreSQL integration requires database {EXPECTED_DATABASE!r}")
    if sentinel != DESTRUCTIVE_SENTINEL:
        raise RuntimeError("destructive PostgreSQL integration sentinel is missing or incorrect")


def _postgres_url() -> str:
    enabled = os.getenv("AINOVEL_POSTGRES_INTEGRATION") == "1"
    database_url = os.getenv("TEST_POSTGRES_URL", "")
    if not enabled or not database_url:
        pytest.skip("set AINOVEL_POSTGRES_INTEGRATION=1 and TEST_POSTGRES_URL to run")
    try:
        _validate_destructive_url(
            database_url,
            sentinel=os.getenv("AINOVEL_POSTGRES_INTEGRATION_SENTINEL"),
        )
    except RuntimeError as exc:
        pytest.fail(str(exc))
    return database_url


@pytest.mark.parametrize(
    ("database_url", "sentinel", "message"),
    [
        (
            "postgresql+psycopg2://ainovel:ainovel@db.example/ainovel_schema_ci",
            DESTRUCTIVE_SENTINEL,
            "loopback host",
        ),
        (
            "postgresql+psycopg2://ainovel:ainovel@localhost/production",
            DESTRUCTIVE_SENTINEL,
            "requires database",
        ),
        (
            "postgresql+psycopg2://ainovel:ainovel@localhost/ainovel_schema_ci",
            "wrong",
            "sentinel",
        ),
    ],
)
def test_destructive_postgres_identity_fails_closed(
    database_url: str,
    sentinel: str,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        _validate_destructive_url(database_url, sentinel=sentinel)


def _upgrade(database_url: str, revision: str) -> None:
    config = _alembic_config(database_url=database_url)
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(config, revision)
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def _alembic_check(database_url: str) -> None:
    config = _alembic_config(database_url=database_url)
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.check(config)
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


@pytest.mark.postgres_integration
def test_pgvector_cleanup_reconciliation_and_alembic_contract() -> None:
    """Exercise the real PostgreSQL-only DDL and the two current migrations."""
    database_url = _postgres_url()
    engine = sa.create_engine(database_url)
    destructive_target_verified = False
    try:
        # This test is destructive by design and only runs against the isolated
        # CI service when explicitly enabled.
        with engine.connect() as connection:
            actual_database = str(connection.exec_driver_sql("SELECT current_database()").scalar_one())
            if actual_database != EXPECTED_DATABASE:
                raise RuntimeError(
                    f"connected database {actual_database!r} does not match destructive target {EXPECTED_DATABASE!r}"
                )
            destructive_target_verified = True
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA public")
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS rogue_contract CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA rogue_contract")
            connection.exec_driver_sql("CREATE DOMAIN public.rogue_domain AS TEXT")

        with pytest.raises(SchemaContractError, match=r"non_public_schemas.*rogue_contract.*user_types.*rogue_domain"):
            check_empty_database(database_url)

        with engine.begin() as connection:
            connection.exec_driver_sql("DROP SCHEMA rogue_contract CASCADE")
            connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA public")

        _upgrade(database_url, PRE_CLEANUP_REVISION)
        with engine.connect() as connection:
            tables = set(sa.inspect(connection).get_table_names())
            assert RETIRED_TABLES.issubset(tables)
            actor_user_id = next(
                column
                for column in sa.inspect(connection).get_columns("batch_generation_tasks")
                if column["name"] == "actor_user_id"
            )
            assert isinstance(actor_user_id["type"], sa.String)
            assert actor_user_id["type"].length == 36

        _upgrade(database_url, "head")
        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            tables = set(inspector.get_table_names())
            assert tables.isdisjoint(RETIRED_TABLES)
            assert "vector_chunks" in tables
            actor_user_id = next(
                column
                for column in inspector.get_columns("batch_generation_tasks")
                if column["name"] == "actor_user_id"
            )
            assert actor_user_id["type"].length == 64
            is_admin = next(column for column in inspector.get_columns("users") if column["name"] == "is_admin")
            assert is_admin["default"] is not None
            current = tuple(MigrationContext.configure(connection).get_current_heads())
            assert current == (HEAD_REVISION,)
            assert migration_head(database_url) == HEAD_REVISION
            # Programmatic equivalent of ``alembic check`` with the same
            # compare-type/default and dialect-specific unmanaged-table filter.
            assert_schema_matches_metadata(connection)
        # Also execute Alembic's public command path so env.py itself is part
        # of the PostgreSQL contract, rather than only the shared primitives.
        _alembic_check(database_url)
    finally:
        if destructive_target_verified:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
                connection.exec_driver_sql("DROP SCHEMA IF EXISTS rogue_contract CASCADE")
        engine.dispose()
