from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from app.core.config import settings
from app.core.logging import log_event
from app.db.session import engine as app_engine

logger = logging.getLogger("ainovel")

INIT_REVISION = "0f24b611cf21"
LEGACY_MULTI_OUTLINE_REVISION = "1c2a0e6b4c2d"
_DEFAULT_PG_MIGRATION_LOCK_ID = 8260228
_DEFAULT_PG_MIGRATION_LOCK_TIMEOUT_SECONDS = 60.0
_DEFAULT_PG_MIGRATION_LOCK_POLL_SECONDS = 0.2

# SHA-256 fingerprints of canonical SQLite schema reflection for real upgrades
# to the only supported unversioned revisions. The signature covers tables,
# column metadata, PK/unique/FK/check constraints, and indexes.
_LEGACY_SQLITE_SCHEMA_REVISIONS: dict[str, str] = {
    INIT_REVISION: "9f375c1f3edcbaa51ce96c530e41bb89119c25c44252fad0ba20aadfd142ada0",
    LEGACY_MULTI_OUTLINE_REVISION: "d8f738cc2bbf219d1060c1b791507846ee749890be781cf16bf7729fa452ed90",
}


def _backend_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def _alembic_config(*, database_url: str) -> Config:
    base_dir = _backend_dir()
    cfg = Config(str(base_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(base_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _normalize_schema_value(value: object) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).split())


def _sorted_schema_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(records, key=lambda record: json.dumps(record, sort_keys=True, separators=(",", ":")))


def _sqlite_schema_fingerprint(conn: Connection, *, tables: set[str]) -> str:
    inspector = inspect(conn)
    schema: dict[str, object] = {}

    for table in sorted(tables):
        columns: list[dict[str, object]] = []
        for column in inspector.get_columns(table):
            column_type = column["type"]
            columns.append(
                {
                    "name": str(column["name"]),
                    "type": str(column_type).upper(),
                    "collation": _normalize_schema_value(getattr(column_type, "collation", None)),
                    "nullable": bool(column["nullable"]),
                    "default": _normalize_schema_value(column.get("default")),
                    "primary_key": int(column.get("primary_key") or 0),
                    "autoincrement": _normalize_schema_value(column.get("autoincrement")),
                    "computed": bool(column.get("computed")),
                    "identity": bool(column.get("identity")),
                }
            )

        primary_key = inspector.get_pk_constraint(table)
        unique_constraints = _sorted_schema_records(
            [
                {
                    "name": constraint.get("name"),
                    "columns": list(constraint.get("column_names") or ()),
                }
                for constraint in inspector.get_unique_constraints(table)
            ]
        )
        indexes = _sorted_schema_records(
            [
                {
                    "name": index.get("name"),
                    "columns": list(index.get("column_names") or ()),
                    "unique": bool(index.get("unique")),
                    "where": _normalize_schema_value((index.get("dialect_options") or {}).get("sqlite_where")),
                }
                for index in inspector.get_indexes(table)
            ]
        )
        foreign_keys = _sorted_schema_records(
            [
                {
                    "name": foreign_key.get("name"),
                    "columns": list(foreign_key.get("constrained_columns") or ()),
                    "referred_schema": foreign_key.get("referred_schema"),
                    "referred_table": foreign_key.get("referred_table"),
                    "referred_columns": list(foreign_key.get("referred_columns") or ()),
                    "onupdate": _normalize_schema_value((foreign_key.get("options") or {}).get("onupdate")),
                    "ondelete": _normalize_schema_value((foreign_key.get("options") or {}).get("ondelete")),
                    "deferrable": (foreign_key.get("options") or {}).get("deferrable"),
                    "initially": _normalize_schema_value((foreign_key.get("options") or {}).get("initially")),
                }
                for foreign_key in inspector.get_foreign_keys(table)
            ]
        )
        checks = _sorted_schema_records(
            [
                {
                    "name": check.get("name"),
                    "sqltext": _normalize_schema_value(check.get("sqltext")),
                }
                for check in inspector.get_check_constraints(table)
            ]
        )
        schema[table] = {
            "columns": columns,
            "primary_key": {
                "name": primary_key.get("name"),
                "columns": list(primary_key.get("constrained_columns") or ()),
            },
            "unique_constraints": unique_constraints,
            "indexes": indexes,
            "foreign_keys": foreign_keys,
            "checks": checks,
        }

    payload = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _legacy_sqlite_revision(schema_fingerprint: str) -> str | None:
    for revision, expected_fingerprint in _LEGACY_SQLITE_SCHEMA_REVISIONS.items():
        if schema_fingerprint == expected_fingerprint:
            return revision
    return None


def _int_env(name: str, *, default: int, min_value: int, max_value: int) -> int:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except Exception:
        return default
    if value < min_value:
        return min_value
    if value > max_value:
        return max_value
    return value


def _float_env(name: str, *, default: float, min_value: float, max_value: float) -> float:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except Exception:
        return default
    if value < min_value:
        return min_value
    if value > max_value:
        return max_value
    return value


def _acquire_pg_migration_lock(conn: Connection) -> None:
    if conn.dialect.name != "postgresql":
        return

    lock_id = _int_env(
        "DB_MIGRATION_LOCK_ID",
        default=_DEFAULT_PG_MIGRATION_LOCK_ID,
        min_value=1,
        max_value=2**31 - 1,
    )
    timeout_s = _float_env(
        "DB_MIGRATION_LOCK_TIMEOUT_SECONDS",
        default=_DEFAULT_PG_MIGRATION_LOCK_TIMEOUT_SECONDS,
        min_value=1.0,
        max_value=600.0,
    )
    poll_s = _float_env(
        "DB_MIGRATION_LOCK_POLL_SECONDS",
        default=_DEFAULT_PG_MIGRATION_LOCK_POLL_SECONDS,
        min_value=0.05,
        max_value=2.0,
    )

    deadline = time.time() + float(timeout_s)
    while True:
        got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id}).scalar())
        if got:
            log_event(logger, "info", event="DB_SCHEMA", action="pg_advisory_lock", lock_id=lock_id)
            return
        if time.time() >= deadline:
            log_event(
                logger,
                "error",
                event="DB_SCHEMA",
                action="pg_advisory_lock_timeout",
                lock_id=lock_id,
                timeout_s=timeout_s,
            )
            raise RuntimeError(f"Failed to acquire Postgres migration lock within {timeout_s:.0f}s (lock_id={lock_id})")
        time.sleep(float(poll_s))


def _release_pg_migration_lock(conn: Connection) -> None:
    if conn.dialect.name != "postgresql":
        return
    lock_id = _int_env(
        "DB_MIGRATION_LOCK_ID",
        default=_DEFAULT_PG_MIGRATION_LOCK_ID,
        min_value=1,
        max_value=2**31 - 1,
    )
    try:
        conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": lock_id})
    except Exception:
        pass


def ensure_db_schema(*, engine: Engine = app_engine) -> None:
    """
    Ensure the DB is usable for the current codebase.

    - If DB is empty/missing: creates all tables via `alembic upgrade head`.
    - If DB is older: upgrades to head.
    - If DB is a recognized legacy SQLite without alembic_version: stamps its exact revision, then upgrades.
    """
    database_url = settings.database_url
    cfg = _alembic_config(database_url=database_url)

    # SQLAlchemy 2.0 will implicitly open a transaction on first execute; if we don't
    # manage it explicitly, `alembic upgrade` may run inside that implicit transaction
    # and then be rolled back when the connection is closed (observed in Docker/PG).
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        _acquire_pg_migration_lock(conn)
        try:
            tables = set(inspect(conn).get_table_names())

            if settings.is_sqlite() and tables and "alembic_version" not in tables:
                schema_fingerprint = _sqlite_schema_fingerprint(conn, tables=tables)
                stamp_target = _legacy_sqlite_revision(schema_fingerprint)
                if settings.app_env == "prod":
                    log_event(
                        logger,
                        "error",
                        event="DB_SCHEMA",
                        action="stamp_skipped",
                        reason="prod_env",
                        target=stamp_target or "unrecognized",
                    )
                    raise RuntimeError(
                        "Detected a legacy SQLite database without alembic_version. "
                        "Automatic `alembic stamp` is disabled in APP_ENV=prod. "
                        "Please backup the DB and run a manual stamp/upgrade."
                    )
                if stamp_target is None:
                    log_event(
                        logger,
                        "error",
                        event="DB_SCHEMA",
                        action="stamp_skipped",
                        reason="unrecognized_schema",
                        tables=sorted(tables),
                    )
                    raise RuntimeError(
                        "Detected an unversioned SQLite database whose schema does not exactly match a supported "
                        "legacy revision. Please backup the DB and run a manual stamp/upgrade."
                    )
                log_event(logger, "warning", event="DB_SCHEMA", action="stamp", target=stamp_target)
                command.stamp(cfg, stamp_target)

            log_event(logger, "info", event="DB_SCHEMA", action="upgrade", target="head")
            command.upgrade(cfg, "head")
        finally:
            _release_pg_migration_lock(conn)
