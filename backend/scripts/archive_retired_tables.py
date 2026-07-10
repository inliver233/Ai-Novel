from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.sql.ddl import sort_tables

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))


ARCHIVE_FORMAT = "ai-novel-retired-tables"
ARCHIVE_VERSION = 1
PURGE_CONFIRMATION = "PURGE_RETIRED_TABLE_DATA"

# backend-data#1: this is the adjudicated set of retired physical tables. Keep
# it explicit: silently widening a destructive maintenance tool is unsafe.
RETIRED_TABLES = (
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
)


class ArchiveVerificationError(RuntimeError):
    """The archive cannot prove that it preserves the current retired data."""


def _canonical_json_bytes(value: object) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (text + "\n").encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _archive_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("retired-table archive does not support non-finite floats")
        return value
    if isinstance(value, datetime):
        return {"$type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"$type": "date", "value": value.isoformat()}
    if isinstance(value, time):
        return {"$type": "time", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"$type": "decimal", "value": str(value)}
    if isinstance(value, UUID):
        return {"$type": "uuid", "value": str(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {"$type": "bytes", "base64": base64.b64encode(raw).decode("ascii")}
    raise TypeError(f"unsupported retired-table value type: {type(value).__name__}")


def _restore_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if not isinstance(value, dict):
        raise ArchiveVerificationError(f"invalid archived value type: {type(value).__name__}")
    value_type = value.get("$type")
    raw = value.get("value")
    try:
        if value_type == "datetime" and isinstance(raw, str):
            return datetime.fromisoformat(raw)
        if value_type == "date" and isinstance(raw, str):
            return date.fromisoformat(raw)
        if value_type == "time" and isinstance(raw, str):
            return time.fromisoformat(raw)
        if value_type == "decimal" and isinstance(raw, str):
            return Decimal(raw)
        if value_type == "uuid" and isinstance(raw, str):
            return UUID(raw)
        if value_type == "bytes" and isinstance(value.get("base64"), str):
            return base64.b64decode(value["base64"], validate=True)
    except (ValueError, TypeError) as exc:
        raise ArchiveVerificationError(f"invalid archived {value_type!r} value") from exc
    raise ArchiveVerificationError(f"unsupported archived value tag: {value_type!r}")


def _require_sqlite_engine(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        raise RuntimeError(
            "retired-table archive/restore/purge supports SQLite only; use pg_dump/pg_restore for PostgreSQL"
        )


def _reflect_retired_tables(conn: Connection) -> dict[str, sa.Table]:
    present = set(sa.inspect(conn).get_table_names()) & set(RETIRED_TABLES)
    metadata = sa.MetaData()
    tables: dict[str, sa.Table] = {}
    for name in RETIRED_TABLES:
        if name in present:
            tables[name] = sa.Table(name, metadata, autoload_with=conn)
    return tables


def _snapshot_table(conn: Connection, name: str, table: sa.Table | None) -> dict[str, Any]:
    if table is None:
        return {
            "columns": [],
            "format_version": ARCHIVE_VERSION,
            "present": False,
            "primary_key": [],
            "rows": [],
            "table": name,
        }

    columns = sorted(table.columns, key=lambda column: column.name)
    primary_key = sorted(table.primary_key.columns, key=lambda column: column.name)
    order_by = primary_key or columns
    statement = sa.select(*columns)
    if order_by:
        statement = statement.order_by(*order_by)
    raw_rows = conn.execute(statement).mappings().all()
    rows = [{column.name: _archive_value(row[column.name]) for column in columns} for row in raw_rows]
    return {
        "columns": [column.name for column in columns],
        "format_version": ARCHIVE_VERSION,
        "present": True,
        "primary_key": [column.name for column in primary_key],
        "rows": rows,
        "table": name,
    }


def _snapshot_all(conn: Connection) -> list[tuple[dict[str, Any], bytes]]:
    reflected = _reflect_retired_tables(conn)
    snapshots: list[tuple[dict[str, Any], bytes]] = []
    for name in RETIRED_TABLES:
        payload = _snapshot_table(conn, name, reflected.get(name))
        snapshots.append((payload, _canonical_json_bytes(payload)))
    return snapshots


def _summary_from_snapshots(snapshots: Sequence[tuple[Mapping[str, Any], bytes]]) -> dict[str, Any]:
    tables = [
        {
            "name": str(payload["table"]),
            "present": bool(payload["present"]),
            "row_count": len(payload["rows"]),
            "sha256": _sha256(content),
        }
        for payload, content in snapshots
    ]
    return {
        "missing_table_count": sum(not table["present"] for table in tables),
        "present_table_count": sum(bool(table["present"]) for table in tables),
        "retired_table_count": len(RETIRED_TABLES),
        "tables": tables,
        "total_rows": sum(int(table["row_count"]) for table in tables),
    }


@contextmanager
def _transaction(conn: Connection, *, write: bool) -> Iterator[None]:
    if write:
        # SQLAlchemy's default deferred BEGIN permits a writer to change data
        # between archive verification and DELETE. BEGIN IMMEDIATE reserves the
        # write lock before verification, keeping the check-and-purge atomic.
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.commit()
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
        return

    with conn.begin():
        yield


def preflight(engine: Engine) -> dict[str, Any]:
    _require_sqlite_engine(engine)
    with engine.connect() as conn, _transaction(conn, write=False):
        snapshots = _snapshot_all(conn)
    return {"action": "preflight", **_summary_from_snapshots(snapshots)}


def _build_manifest(snapshots: Sequence[tuple[Mapping[str, Any], bytes]]) -> dict[str, Any]:
    table_entries = []
    for payload, content in snapshots:
        name = str(payload["table"])
        table_entries.append(
            {
                "file": f"tables/{name}.json",
                "name": name,
                "present": bool(payload["present"]),
                "row_count": len(payload["rows"]),
                "sha256": _sha256(content),
            }
        )
    content_index = [[entry["name"], entry["sha256"]] for entry in table_entries]
    return {
        "archive_content_sha256": _sha256(_canonical_json_bytes(content_index)),
        "format": ARCHIVE_FORMAT,
        "format_version": ARCHIVE_VERSION,
        "retired_table_count": len(RETIRED_TABLES),
        "tables": table_entries,
        "total_rows": sum(int(entry["row_count"]) for entry in table_entries),
    }


def archive(engine: Engine, output_dir: Path) -> dict[str, Any]:
    _require_sqlite_engine(engine)
    destination = output_dir.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"archive destination already exists: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        with engine.connect() as conn, _transaction(conn, write=False):
            snapshots = _snapshot_all(conn)

        tables_dir = temp_dir / "tables"
        tables_dir.mkdir()
        for payload, content in snapshots:
            (tables_dir / f"{payload['table']}.json").write_bytes(content)

        manifest = _build_manifest(snapshots)
        (temp_dir / "manifest.json").write_bytes(_canonical_json_bytes(manifest))
        temp_dir.replace(destination)
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return {
        "action": "archive",
        "archive_content_sha256": manifest["archive_content_sha256"],
        "output_dir": str(destination),
        **_summary_from_snapshots(snapshots),
    }


def _load_verified_archive(archive_dir: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    root = archive_dir.expanduser().resolve()
    manifest_path = root / "manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ArchiveVerificationError(f"cannot read archive manifest: {manifest_path}") from exc

    if not isinstance(manifest, dict):
        raise ArchiveVerificationError("archive manifest must be a JSON object")
    if manifest.get("format") != ARCHIVE_FORMAT or manifest.get("format_version") != ARCHIVE_VERSION:
        raise ArchiveVerificationError("archive format or version is unsupported")

    entries = manifest.get("tables")
    if not isinstance(entries, list):
        raise ArchiveVerificationError("archive manifest tables must be a list")
    names = [entry.get("name") if isinstance(entry, dict) else None for entry in entries]
    if names != list(RETIRED_TABLES):
        raise ArchiveVerificationError(
            "archive manifest does not contain the exact retired-table set in canonical order"
        )
    if manifest.get("retired_table_count") != len(RETIRED_TABLES):
        raise ArchiveVerificationError("archive retired_table_count is invalid")

    contents: dict[str, bytes] = {}
    total_rows = 0
    content_index: list[list[str]] = []
    for entry in entries:
        assert isinstance(entry, dict)
        name = str(entry["name"])
        expected_file = f"tables/{name}.json"
        if entry.get("file") != expected_file:
            raise ArchiveVerificationError(f"archive file path is invalid for table {name}")
        try:
            content = (root / expected_file).read_bytes()
            payload = json.loads(content)
        except (OSError, json.JSONDecodeError) as exc:
            raise ArchiveVerificationError(f"cannot read archived table {name}") from exc
        if _canonical_json_bytes(payload) != content:
            raise ArchiveVerificationError(f"archived table {name} is not canonical JSON")
        checksum = _sha256(content)
        if checksum != entry.get("sha256"):
            raise ArchiveVerificationError(f"archived table {name} checksum mismatch")
        if not isinstance(payload, dict) or payload.get("table") != name:
            raise ArchiveVerificationError(f"archived table {name} payload identity mismatch")
        if payload.get("format_version") != ARCHIVE_VERSION:
            raise ArchiveVerificationError(f"archived table {name} format version mismatch")
        rows = payload.get("rows")
        if not isinstance(rows, list) or len(rows) != entry.get("row_count"):
            raise ArchiveVerificationError(f"archived table {name} row count mismatch")
        if bool(payload.get("present")) != bool(entry.get("present")):
            raise ArchiveVerificationError(f"archived table {name} presence mismatch")
        columns = payload.get("columns")
        primary_key = payload.get("primary_key")
        if (
            not isinstance(columns, list)
            or not all(isinstance(column, str) for column in columns)
            or len(columns) != len(set(columns))
            or not isinstance(primary_key, list)
            or not all(isinstance(column, str) and column in columns for column in primary_key)
        ):
            raise ArchiveVerificationError(f"archived table {name} column metadata is invalid")
        expected_columns = set(columns)
        if any(not isinstance(row, dict) or set(row) != expected_columns for row in rows):
            raise ArchiveVerificationError(f"archived table {name} row columns are invalid")
        total_rows += len(rows)
        contents[name] = content
        content_index.append([name, checksum])

    if total_rows != manifest.get("total_rows"):
        raise ArchiveVerificationError("archive total_rows is invalid")
    content_checksum = _sha256(_canonical_json_bytes(content_index))
    if content_checksum != manifest.get("archive_content_sha256"):
        raise ArchiveVerificationError("archive content checksum mismatch")
    return manifest, contents


def _verify_database_matches_archive(conn: Connection, contents: Mapping[str, bytes]) -> dict[str, sa.Table]:
    reflected = _reflect_retired_tables(conn)
    for name in RETIRED_TABLES:
        payload = _snapshot_table(conn, name, reflected.get(name))
        current_content = _canonical_json_bytes(payload)
        if _sha256(current_content) != _sha256(contents[name]):
            raise ArchiveVerificationError(
                f"database table {name} changed after the archive was created; refusing to purge"
            )
    return reflected


def _fk_safe_delete_order(tables: Mapping[str, sa.Table]) -> list[sa.Table]:
    ordered = sort_tables(sorted(tables.values(), key=lambda table: table.name))
    return list(reversed(ordered))


def _fk_safe_restore_order(tables: Mapping[str, sa.Table]) -> list[sa.Table]:
    return list(sort_tables(sorted(tables.values(), key=lambda table: table.name)))


def purge(
    engine: Engine,
    archive_dir: Path,
    *,
    confirmation: str | None,
    _after_verify: Callable[[], None] | None = None,
) -> dict[str, Any]:
    _require_sqlite_engine(engine)
    if confirmation != PURGE_CONFIRMATION:
        raise PermissionError(f"purge requires --confirm {PURGE_CONFIRMATION}")

    manifest, contents = _load_verified_archive(archive_dir)
    deleted = {name: 0 for name in RETIRED_TABLES}
    with engine.connect() as conn, _transaction(conn, write=True):
        reflected = _verify_database_matches_archive(conn, contents)
        if _after_verify is not None:
            _after_verify()
        for table in _fk_safe_delete_order(reflected):
            result = conn.execute(table.delete())
            deleted[table.name] = max(0, int(result.rowcount or 0))

    return {
        "action": "purge",
        "archive_content_sha256": manifest["archive_content_sha256"],
        "deleted_rows": deleted,
        "total_deleted_rows": sum(deleted.values()),
    }


def restore(engine: Engine, archive_dir: Path) -> dict[str, Any]:
    _require_sqlite_engine(engine)
    manifest, contents = _load_verified_archive(archive_dir)
    restored = {name: 0 for name in RETIRED_TABLES}

    with engine.connect() as conn, _transaction(conn, write=True):
        reflected = _reflect_retired_tables(conn)
        payloads: dict[str, dict[str, Any]] = {}
        for name in RETIRED_TABLES:
            payload = json.loads(contents[name])
            assert isinstance(payload, dict)
            payloads[name] = payload
            archived_present = bool(payload["present"])
            table = reflected.get(name)
            if archived_present != (table is not None):
                raise ArchiveVerificationError(f"database table {name} presence is incompatible with the archive")
            if table is None:
                continue
            archived_columns = list(payload["columns"])
            live_columns = sorted(column.name for column in table.columns)
            archived_primary_key = list(payload["primary_key"])
            live_primary_key = sorted(column.name for column in table.primary_key.columns)
            if archived_columns != live_columns or archived_primary_key != live_primary_key:
                raise ArchiveVerificationError(
                    f"database table {name} columns or primary key are incompatible with the archive"
                )
            row_count = int(conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())
            if row_count != 0:
                raise ArchiveVerificationError(f"database table {name} is not empty; refusing to restore")

        for table in _fk_safe_restore_order(reflected):
            rows = payloads[table.name]["rows"]
            if not rows:
                continue
            decoded_rows = [{column: _restore_value(value) for column, value in row.items()} for row in rows]
            result = conn.execute(table.insert(), decoded_rows)
            restored[table.name] = max(0, int(result.rowcount or 0))

        # Do not call a restore successful merely because INSERT returned. The
        # same canonical snapshots used by purge must match before commit.
        restored_tables = _reflect_retired_tables(conn)
        for name in RETIRED_TABLES:
            current = _canonical_json_bytes(_snapshot_table(conn, name, restored_tables.get(name)))
            if _sha256(current) != _sha256(contents[name]):
                raise ArchiveVerificationError(f"restored table {name} failed checksum verification")

    return {
        "action": "restore",
        "archive_content_sha256": manifest["archive_content_sha256"],
        "restored_rows": restored,
        "total_restored_rows": sum(restored.values()),
    }


def _default_database_url() -> str:
    from app.core.config import settings

    return settings.database_url


def _require_existing_sqlite_database(database_url: str) -> None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return
    database = str(url.database or "").strip()
    if not database or database == ":memory:" or database.startswith("file:"):
        return
    path = Path(database).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {path}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preflight, archive, restore, and explicitly purge the 14 retired SQLite backend-data#1 tables."
    )
    parser.add_argument("--database-url", help="Database URL (defaults to configured DATABASE_URL).")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("preflight", help="Print deterministic row counts and checksums without writing.")

    archive_parser = subparsers.add_parser("archive", help="Write a deterministic, checksummed JSON archive.")
    archive_parser.add_argument("--output", required=True, type=Path, help="New archive directory; must not exist.")

    restore_parser = subparsers.add_parser(
        "restore", help="Verify an archive and atomically restore it into compatible empty tables."
    )
    restore_parser.add_argument("--archive", required=True, type=Path, help="Previously created archive directory.")

    purge_parser = subparsers.add_parser("purge", help="Verify an archive and atomically delete retired-table rows.")
    purge_parser.add_argument("--archive", required=True, type=Path, help="Previously created archive directory.")
    purge_parser.add_argument("--confirm", help=f"Required literal confirmation: {PURGE_CONFIRMATION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = str(args.database_url or _default_database_url()).strip()
    if make_url(database_url).get_backend_name() != "sqlite":
        raise SystemExit("This tool supports SQLite only; use pg_dump/pg_restore for PostgreSQL")
    _require_existing_sqlite_database(database_url)
    engine = sa.create_engine(database_url, pool_pre_ping=True)
    try:
        if args.action == "preflight":
            result = preflight(engine)
        elif args.action == "archive":
            result = archive(engine, args.output)
        elif args.action == "restore":
            result = restore(engine, args.archive)
        else:
            result = purge(engine, args.archive, confirmation=args.confirm)
    finally:
        engine.dispose()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
