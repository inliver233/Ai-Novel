"""Database-backed Alembic schema contract guard.

The guard intentionally runs real migrations.  It proves that the revision
graph has one head, an empty database can reach that head idempotently, and
Alembic autogenerate sees no difference between the resulting schema and the
SQLAlchemy metadata.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Any, Sequence

import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory


BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from app.db.alembic_compare import include_object_for_dialect  # noqa: E402
from app.db.migrations import _alembic_config  # noqa: E402
import app.models  # noqa: E402,F401
from app.db.base import Base  # noqa: E402


class SchemaContractError(RuntimeError):
    """Raised when the migration graph or a database violates the contract."""


@dataclass(frozen=True)
class SchemaContractReport:
    head: str
    current: str
    dialect: str
    differences: tuple[Any, ...]


def _names_or_empty(inspector: sa.Inspector, method_name: str, **kwargs: Any) -> tuple[str, ...]:
    method = getattr(inspector, method_name, None)
    if method is None:
        return ()
    try:
        return tuple(str(name) for name in method(**kwargs))
    except NotImplementedError:
        return ()


def database_user_objects(connection: sa.Connection) -> dict[str, tuple[str, ...]]:
    """Inventory user-created objects that make a database non-empty."""
    inspector = sa.inspect(connection)
    objects: dict[str, tuple[str, ...]] = {
        "tables": tuple(sorted(inspector.get_table_names())),
        "views": tuple(sorted(_names_or_empty(inspector, "get_view_names"))),
        "materialized_views": tuple(sorted(_names_or_empty(inspector, "get_materialized_view_names"))),
        "sequences": tuple(sorted(_names_or_empty(inspector, "get_sequence_names"))),
    }

    if connection.dialect.name == "postgresql":
        schemas = _names_or_empty(inspector, "get_schema_names")
        objects["non_public_schemas"] = tuple(
            sorted(name for name in schemas if name != "public" and name != "information_schema" and not name.startswith("pg_"))
        )
        enums = getattr(inspector, "get_enums", lambda **_kwargs: [])(schema="*")
        domains = getattr(inspector, "get_domains", lambda **_kwargs: [])(schema="*")
        objects["enum_types"] = tuple(
            sorted(f"{item.get('schema')}.{item.get('name')}" for item in enums if item.get("schema") == "public")
        )
        objects["domain_types"] = tuple(
            sorted(f"{item.get('schema')}.{item.get('name')}" for item in domains if item.get("schema") == "public")
        )
        # Inspector APIs cover enums/domains but not every PostgreSQL type
        # class (for example standalone composite/base types).  Query the
        # catalog so those cannot make an allegedly empty database pass.
        public_types = connection.exec_driver_sql(
            """
            SELECT t.typname
            FROM pg_catalog.pg_type AS t
            JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace
            WHERE n.nspname = 'public' AND t.typname NOT LIKE '\\_%' ESCAPE '\\'
            ORDER BY t.typname
            """
        ).scalars()
        objects["user_types"] = tuple(str(name) for name in public_types)

    return {kind: names for kind, names in objects.items() if names}


def migration_head(database_url: str) -> str:
    """Return the sole Alembic head, failing closed for a branched graph."""
    config = _alembic_config(database_url=database_url)
    heads = tuple(ScriptDirectory.from_config(config).get_heads())
    if len(heads) != 1:
        raise SchemaContractError(f"expected exactly one Alembic head, found {len(heads)}: {list(heads)!r}")
    return heads[0]


def schema_differences(
    connection: sa.Connection,
    *,
    metadata: sa.MetaData = Base.metadata,
) -> tuple[Any, ...]:
    """Return metadata drift using the same strict options as ``env.py``."""
    dialect_name = connection.dialect.name
    context = MigrationContext.configure(
        connection,
        opts={
            "compare_type": True,
            "compare_server_default": True,
            "include_object": include_object_for_dialect(dialect_name),
            "render_as_batch": dialect_name == "sqlite",
        },
    )
    return tuple(compare_metadata(context, metadata))


def assert_schema_matches_metadata(
    connection: sa.Connection,
    *,
    metadata: sa.MetaData = Base.metadata,
) -> None:
    """Raise with Alembic's concrete diff when the live schema has drifted."""
    differences = schema_differences(connection, metadata=metadata)
    if differences:
        rendered = "\n".join(f"  - {difference!r}" for difference in differences)
        raise SchemaContractError(f"database schema differs from model metadata:\n{rendered}")


def check_empty_database(database_url: str) -> SchemaContractReport:
    """Run the complete schema contract against an empty database.

    The database is caller-owned and deliberately not dropped by this helper.
    Refusing a non-empty database prevents a green result from an accidentally
    pre-provisioned CI service.
    """
    head = migration_head(database_url)
    engine = sa.create_engine(database_url)
    try:
        with engine.connect() as connection:
            existing = database_user_objects(connection)
        if existing:
            details = "; ".join(f"{kind}={list(names)!r}" for kind, names in sorted(existing.items()))
            raise SchemaContractError("schema contract requires an empty database; found user objects: " + details)

        config = _alembic_config(database_url=database_url)
        previous_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = database_url
        try:
            command.upgrade(config, "head")
            # A second real invocation proves upgrade-to-head is idempotent.
            command.upgrade(config, "head")
        finally:
            if previous_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous_url

        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            current_heads = tuple(context.get_current_heads())
            if current_heads != (head,):
                raise SchemaContractError(f"database current revisions {current_heads!r} do not equal head {head!r}")
            differences = schema_differences(connection)
            if differences:
                rendered = "\n".join(f"  - {difference!r}" for difference in differences)
                raise SchemaContractError(f"database schema differs from model metadata:\n{rendered}")
            return SchemaContractReport(
                head=head,
                current=current_heads[0],
                dialect=connection.dialect.name,
                differences=differences,
            )
    finally:
        engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", "sqlite:///./alembic-contract.db"),
        help="URL of an empty, disposable database (default: DATABASE_URL)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = check_empty_database(args.database_url)
    except SchemaContractError as exc:
        print(f"[alembic-check] FAIL: {exc}", file=sys.stderr)
        return 2
    print(f"[alembic-check] head={report.head}")
    print(f"[alembic-check] current={report.current}")
    print(f"[alembic-check] dialect={report.dialect}")
    print("[alembic-check] autogenerate_differences=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
