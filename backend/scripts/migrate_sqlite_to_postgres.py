from __future__ import annotations

import argparse
import base64
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
import tempfile
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import Table
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.sql.ddl import sort_tables

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))


_IGNORED_SOURCE_TABLES = frozenset(
    {
        "alembic_version",
        # SQLite-only FTS5 virtual table and its derived shadow tables. The
        # portable source of truth is search_documents; Postgres does not use
        # these physical tables.
        "search_index",
        "search_index_config",
        "search_index_data",
        "search_index_docsize",
        "search_index_idx",
    }
)

# Legacy databases can contain these retired application tables even though
# the current target schema intentionally no longer does. They are not generic
# metadata: data in any of them must be archived before migration, while empty
# tables can be skipped explicitly.
_RETIRED_SOURCE_TABLES = frozenset(
    {
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
)

_ACTIVE_REPORT: dict[str, Any] | None = None
_ACTIVE_REPORT_PATH: str | None = None


def _backend_alembic_config(*, database_url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _mask_db_url(database_url: str) -> str:
    raw = (database_url or "").strip()
    try:
        url = make_url(raw)
    except Exception:
        return "<invalid-url>"
    if url.password:
        url = url.set(password="***")
    return str(url)


def _sanitize_failure_message(message: str) -> str:
    return re.sub(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@]+(@)", r"\1***\2", str(message or ""), flags=re.I)


def _normalize_sqlite_url(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise SystemExit("--source is required")
    try:
        if make_url(raw).get_backend_name() == "sqlite":
            return raw
    except Exception:
        pass
    path = Path(raw).expanduser().resolve()
    return f"sqlite:///{path.as_posix()}"


def _require_existing_sqlite_source(source_url: str) -> Path:
    try:
        url = make_url(source_url)
    except Exception as exc:
        raise SystemExit("--source must be a valid SQLite URL or file path") from exc
    if url.get_backend_name() != "sqlite":
        raise SystemExit(f"--source must be SQLite, got dialect={url.get_backend_name()!r}")

    database = str(url.database or "").strip()
    if not database or database == ":memory:" or database.startswith("file:"):
        raise SystemExit("--source must point to an existing SQLite database file")
    source_path = Path(database).expanduser()
    if not source_path.is_absolute():
        source_path = (Path.cwd() / source_path).resolve()
    if not source_path.is_file():
        raise SystemExit(f"SQLite source database does not exist: {source_path}")
    return source_path


def _validate_source_schema(source: Connection | Engine) -> None:
    if source.dialect.name != "sqlite":
        raise RuntimeError(f"Source engine must be SQLite, got dialect={source.dialect.name!r}")
    try:
        portable_tables = set(sa.inspect(source).get_table_names()) - _IGNORED_SOURCE_TABLES
    except sa.exc.SQLAlchemyError as exc:
        raise RuntimeError("Unable to read the SQLite source schema") from exc

    required_anchors = {"users", "projects"}
    missing_anchors = sorted(required_anchors - portable_tables)
    if not portable_tables or missing_anchors:
        raise RuntimeError(
            "SQLite source does not contain a recognizable Ai-Novel schema; "
            f"missing required tables: {missing_anchors or sorted(required_anchors)}"
        )


def _is_deferred_project_outline_fk(foreign_key: sa.ForeignKey) -> bool:
    return (
        foreign_key.parent.table.name == "projects"
        and foreign_key.parent.name == "active_outline_id"
        and foreign_key.column.table.name == "outlines"
    )


def _sorted_table_names(tables: Iterable[Table]) -> list[str]:
    ordered_input = sorted(tables, key=lambda table: table.name)
    return [table.name for table in sort_tables(ordered_input, skip_fn=_is_deferred_project_outline_fk)]


@contextmanager
def _locked_sqlite_source(src_engine: Engine) -> Iterator[Connection]:
    """Hold one SQLite writer lock from retired preflight through verification."""
    if src_engine.dialect.name != "sqlite":
        raise RuntimeError(f"Source engine must be SQLite, got dialect={src_engine.dialect.name!r}")
    with src_engine.connect() as source_connection:
        source_connection.commit()
        source_connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield source_connection
        except BaseException:
            source_connection.rollback()
            raise
        else:
            source_connection.commit()


def _build_copy_plan(
    *,
    src_connection: Connection,
    dst_engine: Engine,
) -> tuple[list[str], dict[str, Table], dict[str, Table], list[str], list[str]]:
    source_table_names = set(sa.inspect(src_connection).get_table_names())
    target_table_names = set(sa.inspect(dst_engine).get_table_names()) - {"alembic_version"}
    skipped_metadata_tables = sorted(source_table_names & _IGNORED_SOURCE_TABLES)
    skipped_retired_tables = sorted(source_table_names & _RETIRED_SOURCE_TABLES)

    if skipped_retired_tables:
        retired_counts: list[tuple[str, int]] = []
        source_metadata = sa.MetaData()
        source_metadata.reflect(bind=src_connection, only=skipped_retired_tables)
        for table_name in skipped_retired_tables:
            count = _count_rows(src_connection, source_metadata.tables[table_name])
            if count:
                retired_counts.append((table_name, count))
        if retired_counts:
            counts = ", ".join(f"{name}={count}" for name, count in retired_counts)
            raise RuntimeError(
                "SQLite source contains data in retired tables and migration aborted before copying "
                f"({counts}). Run `python scripts/archive_retired_tables.py --database-url <source-url> archive "
                "--output <archive-dir>` and then `python scripts/archive_retired_tables.py --database-url "
                "<source-url> purge --archive <archive-dir> --confirm PURGE_RETIRED_TABLE_DATA`; retry only after "
                "every retired source table is empty."
            )

    portable_source_tables = source_table_names - _IGNORED_SOURCE_TABLES - _RETIRED_SOURCE_TABLES

    unmigrated_source_tables = sorted(portable_source_tables - target_table_names)
    if unmigrated_source_tables:
        raise RuntimeError(
            "Source contains application tables that do not exist in the target schema; migration aborted before "
            f"copying data: {unmigrated_source_tables}"
        )

    copy_table_names = portable_source_tables & target_table_names
    src_md = sa.MetaData()
    dst_md = sa.MetaData()
    if copy_table_names:
        names = sorted(copy_table_names)
        src_md.reflect(bind=src_connection, only=names)
        dst_md.reflect(bind=dst_engine, only=names)

    src_tables = {name: src_md.tables[name] for name in copy_table_names}
    dst_tables = {name: dst_md.tables[name] for name in copy_table_names}
    table_order = _sorted_table_names(dst_tables.values())
    return table_order, src_tables, dst_tables, skipped_metadata_tables, skipped_retired_tables


def _upgrade_to_head(cfg: Config, *, database_url: str) -> None:
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(cfg, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def _ensure_target_migrations(target_url: str) -> None:
    cfg = _backend_alembic_config(database_url=target_url)
    _upgrade_to_head(cfg, database_url=target_url)


def _create_head_schema_reference_engine() -> Engine:
    database_url = "sqlite://"
    engine = sa.create_engine(database_url, hide_parameters=True)
    cfg = _backend_alembic_config(database_url=database_url)
    try:
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            _upgrade_to_head(cfg, database_url=database_url)
    except Exception:
        engine.dispose()
        raise
    return engine


def _pg_required_extensions(engine: Engine) -> dict[str, bool] | None:
    if engine.dialect.name != "postgresql":
        return None
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT extname FROM pg_extension WHERE extname IN ('uuid-ossp', 'pg_trgm', 'vector') ORDER BY extname"
            )
        ).fetchall()
    existing = {str(r[0]) for r in rows}
    return {
        "uuid-ossp": "uuid-ossp" in existing,
        "pg_trgm": "pg_trgm" in existing,
        "vector": "vector" in existing,
    }


def _missing_required_extensions(extensions: dict[str, bool] | None) -> list[str]:
    if extensions is None:
        return []
    return sorted(name for name, available in extensions.items() if not available)


def _count_rows(conn: sa.Connection, table: Table) -> int:
    return int(conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())


def _ordered_select(table: Table):  # type: ignore[no-untyped-def]
    pk_cols = list(table.primary_key.columns)
    order_by = pk_cols if pk_cols else [table.c[column.name] for column in table.columns]
    return sa.select(table).order_by(*order_by)


def _normalize_digest_value(value: object, column_type: sa.types.TypeEngine[Any]) -> object:
    if value is None:
        return None
    if isinstance(column_type, sa.Boolean):
        return bool(value)
    if isinstance(column_type, sa.DateTime):
        parsed = value
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                return value
        if isinstance(parsed, datetime):
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
            return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(column_type, sa.Date):
        if isinstance(value, str):
            try:
                return date.fromisoformat(value.strip()).isoformat()
            except ValueError:
                return value
        if isinstance(value, date):
            return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return format(value, ".17g")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"base64": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, UUID):
        return str(value)
    return value


def _table_digest(
    conn: sa.Connection,
    table: Table,
    *,
    canonical_table: Table | None = None,
    chunk_size: int = 2000,
) -> str:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    canonical_columns = (canonical_table if canonical_table is not None else table).c
    row_count = 0
    row_hash_sum = 0
    row_hash_xor = 0
    modulus = 1 << 256
    result = conn.execute(_ordered_select(table))
    while True:
        batch = result.mappings().fetchmany(chunk_size)
        if not batch:
            break
        for row in batch:
            normalized = {
                column.name: _normalize_digest_value(row[column.name], canonical_columns[column.name].type)
                for column in table.columns
            }
            encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            row_hash = int.from_bytes(hashlib.sha256(encoded).digest(), "big")
            row_count += 1
            row_hash_sum = (row_hash_sum + row_hash) % modulus
            row_hash_xor ^= row_hash
    manifest = f"v1:{row_count}:{row_hash_sum:064x}:{row_hash_xor:064x}"
    return hashlib.sha256(manifest.encode("ascii")).hexdigest()


def _missing_fk_count(conn: sa.Connection, table: Table, fk: dict[str, Any], md: sa.MetaData) -> int:
    constrained = fk.get("constrained_columns") or []
    referred_cols = fk.get("referred_columns") or []
    referred_table_name = fk.get("referred_table")
    referred_schema = fk.get("referred_schema")
    if not constrained or len(constrained) != len(referred_cols) or not referred_table_name:
        raise RuntimeError(f"Unsupported foreign key metadata on table {table.name}: {fk}")
    metadata_key = f"{referred_schema}.{referred_table_name}" if referred_schema else referred_table_name
    ref_table = md.tables.get(metadata_key)
    if ref_table is None:
        ref_table = Table(referred_table_name, md, schema=referred_schema, autoload_with=conn)
    ref_relation = ref_table.alias(f"fk_ref_{table.name}_{referred_table_name}")

    join_condition = sa.and_(
        *(table.c[fk_col] == ref_relation.c[ref_col] for fk_col, ref_col in zip(constrained, referred_cols))
    )
    non_null_condition = sa.and_(*(table.c[column].is_not(None) for column in constrained))
    missing_condition = sa.and_(*(ref_relation.c[column].is_(None) for column in referred_cols))
    stmt = (
        sa.select(sa.func.count())
        .select_from(table.outerjoin(ref_relation, join_condition))
        .where(non_null_condition)
        .where(missing_condition)
    )
    return int(conn.execute(stmt).scalar_one())


def _reset_postgres_sequence(conn: sa.Connection, table: Table) -> None:
    if conn.dialect.name != "postgresql":
        return

    primary_key_columns = list(table.primary_key.columns)
    if len(primary_key_columns) != 1 or not isinstance(primary_key_columns[0].type, sa.Integer):
        return

    primary_key = primary_key_columns[0]
    sequence_name = conn.execute(
        sa.text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
        {"table_name": table.fullname, "column_name": primary_key.name},
    ).scalar_one_or_none()
    if not sequence_name:
        return

    max_value = conn.execute(sa.select(sa.func.max(primary_key))).scalar_one()
    conn.execute(
        sa.text("SELECT setval(CAST(:sequence_name AS regclass), :value, :is_called)"),
        {
            "sequence_name": str(sequence_name),
            "value": int(max_value) if max_value is not None else 1,
            "is_called": max_value is not None,
        },
    )


def _write_report(path: str | Path, report: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)
    temp_path.replace(destination)


def _report_has_inserted_rows(report: dict[str, Any]) -> bool:
    return any(
        int(table.get("inserted") or 0) > 0 for table in report.get("tables", {}).values() if isinstance(table, dict)
    )


def _copy_table(
    *,
    src_conn: sa.Connection,
    dst_engine: Engine,
    src_table: Table,
    dst_table: Table,
    chunk_size: int,
    resume: bool,
    post_insert_hook: Callable[[sa.Connection], None] | None = None,
) -> dict[str, int]:
    attempted = 0
    inserted = 0

    if resume and dst_engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        pk_cols = [c.name for c in dst_table.primary_key.columns]
        if not pk_cols:
            raise RuntimeError(f"Table has no primary key, cannot resume safely: {dst_table.name}")
        insert_stmt = pg_insert(dst_table).on_conflict_do_nothing(index_elements=pk_cols)
    else:
        insert_stmt = dst_table.insert()

    with dst_engine.begin() as dst_conn:
        if dst_conn.dialect.name == "postgresql":
            dst_conn.exec_driver_sql("SET LOCAL TIME ZONE 'UTC'")
        result = src_conn.execute(sa.select(src_table))
        while True:
            batch = result.mappings().fetchmany(chunk_size)
            if not batch:
                break
            rows = [dict(r) for r in batch]
            if rows:
                attempted += len(rows)
                execution = dst_conn.execute(insert_stmt, rows)
                inserted += max(0, int(getattr(execution, "rowcount", len(rows)) or 0))
        if post_insert_hook is not None:
            post_insert_hook(dst_conn)
        _reset_postgres_sequence(dst_conn, dst_table)
    return {"attempted": attempted, "inserted": inserted, "skipped": attempted - inserted}


def _run_main(argv: list[str] | None = None) -> int:
    global _ACTIVE_REPORT, _ACTIVE_REPORT_PATH
    parser = argparse.ArgumentParser(description="Migrate ainovel SQLite data to Postgres (preserve IDs).")
    parser.add_argument("--source", required=True, help="SQLite DB path or sqlite:/// URL")
    parser.add_argument(
        "--target", required=True, help="Postgres SQLAlchemy URL (e.g. postgresql://user:pass@host:5432/db)"
    )
    parser.add_argument("--chunk-size", type=int, default=2000, help="Insert batch size per table")
    parser.add_argument("--no-migrate-schema", action="store_true", help="Skip alembic upgrade head on target")
    parser.add_argument(
        "--resume", action="store_true", help="Idempotent mode: ON CONFLICT DO NOTHING (for retry/resume)"
    )
    parser.add_argument("--report", default="sqlite_to_postgres.report.json", help="Write a JSON report to this path")
    parser.add_argument("--dry-run", action="store_true", help="Plan only; do not write to target")
    args = parser.parse_args(argv)

    src_url = _normalize_sqlite_url(args.source)
    target_url = str((args.target or "").strip())
    if not target_url:
        raise SystemExit("--target is required")

    report: dict[str, Any] = {
        "status": "running",
        "partial": False,
        "source": {"url": _mask_db_url(src_url)},
        "target": {"url": _mask_db_url(target_url)},
        "tables": {},
        "warnings": [],
        "verification": {"status": "pending", "failures": []},
    }
    _ACTIVE_REPORT = report
    _ACTIVE_REPORT_PATH = str(args.report)
    _write_report(args.report, report)
    if int(args.chunk_size) <= 0:
        raise SystemExit("--chunk-size must be greater than zero")

    _require_existing_sqlite_source(src_url)
    print(f"[source] {_mask_db_url(src_url)}")
    print(f"[target] {_mask_db_url(target_url)}")

    src_engine = sa.create_engine(src_url, connect_args={"check_same_thread": False}, hide_parameters=True)
    try:
        _validate_source_schema(src_engine)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    dst_engine = sa.create_engine(target_url, pool_pre_ping=True, hide_parameters=True)

    if dst_engine.dialect.name != "postgresql":
        raise SystemExit(f"--target must be Postgres, got dialect={dst_engine.dialect.name!r}")

    plan_target_engine = dst_engine
    reference_engine: Engine | None = None
    if not args.no_migrate_schema:
        if args.dry_run:
            print("[plan] alembic upgrade head (target; simulated locally without target writes)")
            reference_engine = _create_head_schema_reference_engine()
            plan_target_engine = reference_engine
        else:
            print("[step] alembic upgrade head (target)")
            _ensure_target_migrations(target_url)

    exts = _pg_required_extensions(dst_engine)
    if exts is not None:
        report["postgres_extensions"] = exts
        missing_extensions = _missing_required_extensions(exts)
        if missing_extensions:
            report["verification"] = {
                "status": "failed",
                "failures": [{"check": "postgres_extensions", "missing": missing_extensions, "details": exts}],
            }
            _write_report(args.report, report)
            raise RuntimeError(f"Required PostgreSQL extensions are missing: {missing_extensions}")

    with _locked_sqlite_source(src_engine) as src_conn:
        try:
            try:
                table_order, src_tables, dst_tables, skipped_metadata_tables, skipped_retired_tables = _build_copy_plan(
                    src_connection=src_conn,
                    dst_engine=plan_target_engine,
                )
            except RuntimeError as exc:
                raise SystemExit(str(exc)) from exc
        finally:
            if reference_engine is not None:
                reference_engine.dispose()

        report["source"]["skipped_metadata_tables"] = skipped_metadata_tables
        report["source"]["skipped_empty_retired_tables"] = skipped_retired_tables
        report["table_order"] = table_order
        if skipped_metadata_tables:
            print(f"[info] skipped source metadata/derived tables: {skipped_metadata_tables}")
        if skipped_retired_tables:
            print(f"[info] skipped verified-empty retired source tables: {skipped_retired_tables}")

        # Safety check: by default require empty target so we don't duplicate real data.
        if not args.resume and not args.dry_run:
            with dst_engine.connect() as conn:
                non_empty: list[tuple[str, int]] = []
                for name in table_order:
                    cnt = _count_rows(conn, dst_tables[name])
                    if cnt:
                        non_empty.append((name, cnt))
                if non_empty:
                    raise SystemExit(
                        "Target DB is not empty. Use --resume to continue a partial run.\n"
                        + "\n".join([f"- {t} count={c}" for t, c in non_empty])
                    )

        # projects has a FK cycle with outlines via projects.active_outline_id (nullable).
        projects_active_outline_by_project_id: dict[str, str] = {}

        def _projects_post_insert_hook(conn: sa.Connection) -> None:
            if not projects_active_outline_by_project_id:
                return
            t_projects = dst_tables["projects"]
            for project_id, outline_id in projects_active_outline_by_project_id.items():
                conn.execute(
                    sa.update(t_projects).where(t_projects.c.id == project_id).values(active_outline_id=outline_id)
                )

        if args.dry_run:
            print("[plan] table copy order:")
            for t in table_order:
                print(f"  - {t}")
            report["status"] = "dry_run"
            report["verification"] = {"status": "skipped", "failures": []}
            _write_report(args.report, report)
            return 0

        for name in table_order:
            src_table = src_tables[name]
            dst_table = dst_tables[name]

            print(f"[step] copy table={name}")

            if name == "projects":
                # Copy projects with active_outline_id cleared, then restore after outlines are copied.
                attempted = 0
                inserted = 0
                if args.resume and dst_engine.dialect.name == "postgresql":
                    from sqlalchemy.dialects.postgresql import insert as pg_insert

                    pk_cols = [c.name for c in dst_table.primary_key.columns]
                    insert_stmt = pg_insert(dst_table).on_conflict_do_nothing(index_elements=pk_cols)
                else:
                    insert_stmt = dst_table.insert()

                with dst_engine.begin() as dst_conn:
                    if dst_conn.dialect.name == "postgresql":
                        dst_conn.exec_driver_sql("SET LOCAL TIME ZONE 'UTC'")
                    result = src_conn.execute(sa.select(src_table))
                    while True:
                        batch = result.mappings().fetchmany(int(args.chunk_size))
                        if not batch:
                            break
                        rows: list[dict[str, Any]] = []
                        for r in batch:
                            row = dict(r)
                            active_outline_id = row.get("active_outline_id")
                            if active_outline_id:
                                projects_active_outline_by_project_id[str(row["id"])] = str(active_outline_id)
                            row["active_outline_id"] = None
                            rows.append(row)
                        if rows:
                            attempted += len(rows)
                            execution = dst_conn.execute(insert_stmt, rows)
                            inserted += max(0, int(getattr(execution, "rowcount", len(rows)) or 0))
                report["tables"][name] = {
                    "attempted": attempted,
                    "inserted": inserted,
                    "skipped": attempted - inserted,
                }
                _write_report(args.report, report)
                continue

            post_insert_hook = None
            if name == "outlines":
                post_insert_hook = _projects_post_insert_hook

            copy_stats = _copy_table(
                src_conn=src_conn,
                dst_engine=dst_engine,
                src_table=src_table,
                dst_table=dst_table,
                chunk_size=int(args.chunk_size),
                resume=bool(args.resume),
                post_insert_hook=post_insert_hook,
            )
            report["tables"][name] = copy_stats
            _write_report(args.report, report)

        # Verification uses the same locked source connection as preflight and
        # copy, so counts and hashes cannot observe a later writer's snapshot.
        verification_failures: list[dict[str, Any]] = []
        with dst_engine.connect().execution_options(isolation_level="REPEATABLE READ") as dconn:
            with dconn.begin():
                if dconn.dialect.name == "postgresql":
                    dconn.exec_driver_sql("SET LOCAL TIME ZONE 'UTC'")
                for name in table_order:
                    st = src_tables[name]
                    dt = dst_tables[name]

                    src_count = _count_rows(src_conn, st)
                    dst_count = _count_rows(dconn, dt)

                    src_digest = _table_digest(src_conn, st, canonical_table=dt, chunk_size=int(args.chunk_size))
                    dst_digest = _table_digest(dconn, dt, canonical_table=dt, chunk_size=int(args.chunk_size))

                    table_report = report["tables"].setdefault(name, {})
                    table_report.update(
                        {
                            "source_count": src_count,
                            "target_count": dst_count,
                            "digest_source": src_digest,
                            "digest_target": dst_digest,
                        }
                    )

                    fk_missing_total = 0
                    inspector = sa.inspect(dconn)
                    for fk in inspector.get_foreign_keys(name):
                        fk_missing_total += _missing_fk_count(dconn, dt, fk, dt.metadata)
                    table_report["missing_fk_total"] = fk_missing_total

                    if src_count != dst_count:
                        verification_failures.append(
                            {"table": name, "check": "count", "source": src_count, "target": dst_count}
                        )
                    if src_digest != dst_digest:
                        verification_failures.append(
                            {"table": name, "check": "digest", "source": src_digest, "target": dst_digest}
                        )
                    if fk_missing_total:
                        verification_failures.append(
                            {"table": name, "check": "foreign_keys", "missing": fk_missing_total}
                        )

        report["verification"] = {
            "status": "failed" if verification_failures else "passed",
            "failures": verification_failures,
        }
        if verification_failures:
            report["status"] = "failed"
            report["partial"] = _report_has_inserted_rows(report)
            _write_report(args.report, report)
            raise RuntimeError(f"Verification failed with {len(verification_failures)} hard mismatch(es)")

    report["status"] = "succeeded"
    report["partial"] = False
    _write_report(args.report, report)
    print(f"[ok] wrote report: {args.report}")
    return 0


def main(argv: list[str] | None = None) -> int:
    global _ACTIVE_REPORT, _ACTIVE_REPORT_PATH
    _ACTIVE_REPORT = None
    _ACTIVE_REPORT_PATH = None
    try:
        return _run_main(argv)
    except SystemExit:
        if _ACTIVE_REPORT is None:
            raise
        exc = sys.exc_info()[1]
        message = str(exc)
    except sa.exc.SQLAlchemyError as exc:
        message = f"Database operation failed ({type(exc).__name__})"
    except Exception as exc:
        message = str(exc) or type(exc).__name__

    message = _sanitize_failure_message(message)
    print(f"[fail] {message}", file=sys.stderr)
    if _ACTIVE_REPORT is not None and _ACTIVE_REPORT_PATH is not None:
        _ACTIVE_REPORT["status"] = "failed"
        _ACTIVE_REPORT["partial"] = _report_has_inserted_rows(_ACTIVE_REPORT)
        verification = _ACTIVE_REPORT.setdefault("verification", {"status": "failed", "failures": []})
        verification["status"] = "failed"
        failures = verification.setdefault("failures", [])
        if not failures:
            failures.append({"check": "migration", "message": message})
        try:
            _write_report(_ACTIVE_REPORT_PATH, _ACTIVE_REPORT)
        except Exception:
            print("[fail] Unable to persist failure report", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
