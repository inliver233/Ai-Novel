from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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
        return raw
    if url.password:
        url = url.set(password="***")
    return str(url)


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
    engine = sa.create_engine(database_url)
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
            sa.text("SELECT extname FROM pg_extension WHERE extname IN ('uuid-ossp', 'pg_trgm') ORDER BY extname")
        ).fetchall()
    existing = {str(r[0]) for r in rows}
    return {"uuid-ossp": "uuid-ossp" in existing, "pg_trgm": "pg_trgm" in existing}


def _count_rows(conn: sa.Connection, table: Table) -> int:
    return int(conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())


def _select_samples(conn: sa.Connection, table: Table, *, limit: int) -> list[dict[str, Any]]:
    pk_cols = list(table.primary_key.columns)
    order_by = pk_cols if pk_cols else [table.c[c.name] for c in table.columns]
    rows = conn.execute(sa.select(table).order_by(*order_by).limit(max(0, int(limit)))).mappings().all()
    return [dict(r) for r in rows]


def _sample_hash(rows: list[dict[str, Any]]) -> str:
    def _default(v: object) -> str:
        return str(v)

    txt = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=_default)
    return hashlib.sha256(txt.encode("utf-8")).hexdigest()


def _missing_fk_count(conn: sa.Connection, table: Table, fk: dict[str, Any], md: sa.MetaData) -> int:
    constrained = fk.get("constrained_columns") or []
    referred_cols = fk.get("referred_columns") or []
    referred_table_name = fk.get("referred_table")
    if len(constrained) != 1 or len(referred_cols) != 1 or not referred_table_name:
        return 0

    fk_col = constrained[0]
    ref_col = referred_cols[0]
    ref_table = md.tables.get(referred_table_name)
    if ref_table is None:
        ref_table = Table(referred_table_name, md, autoload_with=conn)

    stmt = (
        sa.select(sa.func.count())
        .select_from(table.outerjoin(ref_table, table.c[fk_col] == ref_table.c[ref_col]))
        .where(table.c[fk_col].is_not(None))
        .where(ref_table.c[ref_col].is_(None))
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


def _copy_table(
    *,
    src_conn: sa.Connection,
    dst_engine: Engine,
    src_table: Table,
    dst_table: Table,
    chunk_size: int,
    resume: bool,
    post_insert_hook: Callable[[sa.Connection], None] | None = None,
) -> int:
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
        result = src_conn.execute(sa.select(src_table))
        while True:
            batch = result.mappings().fetchmany(chunk_size)
            if not batch:
                break
            rows = [dict(r) for r in batch]
            if rows:
                dst_conn.execute(insert_stmt, rows)
                inserted += len(rows)
        if post_insert_hook is not None:
            post_insert_hook(dst_conn)
        _reset_postgres_sequence(dst_conn, dst_table)
    return inserted


def main(argv: list[str] | None = None) -> int:
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

    _require_existing_sqlite_source(src_url)
    print(f"[source] {_mask_db_url(src_url)}")
    print(f"[target] {_mask_db_url(target_url)}")

    src_engine = sa.create_engine(src_url, connect_args={"check_same_thread": False})
    try:
        _validate_source_schema(src_engine)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    dst_engine = sa.create_engine(target_url, pool_pre_ping=True)

    if dst_engine.dialect.name != "postgresql":
        raise SystemExit(f"--target must be Postgres, got dialect={dst_engine.dialect.name!r}")

    report: dict[str, Any] = {
        "source": {"url": _mask_db_url(src_url)},
        "target": {"url": _mask_db_url(target_url)},
        "tables": {},
        "warnings": [],
    }

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
        if not all(exts.values()):
            report["warnings"].append({"code": "PG_EXT_MISSING", "details": exts})
            print(f"[warn] postgres extensions missing: {exts}")

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
            return 0

        for name in table_order:
            src_table = src_tables[name]
            dst_table = dst_tables[name]

            print(f"[step] copy table={name}")

            if name == "projects":
                # Copy projects with active_outline_id cleared, then restore after outlines are copied.
                inserted = 0
                if args.resume and dst_engine.dialect.name == "postgresql":
                    from sqlalchemy.dialects.postgresql import insert as pg_insert

                    pk_cols = [c.name for c in dst_table.primary_key.columns]
                    insert_stmt = pg_insert(dst_table).on_conflict_do_nothing(index_elements=pk_cols)
                else:
                    insert_stmt = dst_table.insert()

                with dst_engine.begin() as dst_conn:
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
                            dst_conn.execute(insert_stmt, rows)
                            inserted += len(rows)
                report["tables"][name] = {"inserted": inserted}
                continue

            post_insert_hook = None
            if name == "outlines":
                post_insert_hook = _projects_post_insert_hook

            inserted = _copy_table(
                src_conn=src_conn,
                dst_engine=dst_engine,
                src_table=src_table,
                dst_table=dst_table,
                chunk_size=int(args.chunk_size),
                resume=bool(args.resume),
                post_insert_hook=post_insert_hook,
            )
            report["tables"][name] = {"inserted": inserted}

        # Verification uses the same locked source connection as preflight and
        # copy, so counts and hashes cannot observe a later writer's snapshot.
        sample_limit = 20
        with dst_engine.connect() as dconn:
            for name in table_order:
                st = src_tables[name]
                dt = dst_tables[name]

                src_count = _count_rows(src_conn, st)
                dst_count = _count_rows(dconn, dt)

                src_samples = _select_samples(src_conn, st, limit=sample_limit)
                dst_samples = _select_samples(dconn, dt, limit=sample_limit)

                table_report = report["tables"].setdefault(name, {})
                table_report.update(
                    {
                        "source_count": src_count,
                        "target_count": dst_count,
                        "sample_hash_source": _sample_hash(src_samples),
                        "sample_hash_target": _sample_hash(dst_samples),
                    }
                )

                fk_missing_total = 0
                inspector = sa.inspect(dconn)
                for fk in inspector.get_foreign_keys(name):
                    fk_missing_total += _missing_fk_count(dconn, dt, fk, dt.metadata)
                table_report["missing_fk_total"] = fk_missing_total

    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(f"[ok] wrote report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
