from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import inspect
import json
from pathlib import Path

import pytest
import sqlalchemy as sa


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_sqlite_to_postgres.py"
_SPEC = importlib.util.spec_from_file_location("_migrate_verification", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
migration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migration)


def _table(engine: sa.Engine, *, name: str = "records") -> sa.Table:
    metadata = sa.MetaData()
    table = sa.Table(
        name,
        metadata,
        sa.Column("tenant_id", sa.String(16), primary_key=True),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.LargeBinary, nullable=True),
    )
    metadata.create_all(engine)
    return table


def test_full_digest_detects_change_after_twentieth_row() -> None:
    source = sa.create_engine("sqlite://")
    target = sa.create_engine("sqlite://")
    source_table = _table(source)
    target_table = _table(target)
    rows = [
        {
            "tenant_id": "t",
            "id": index,
            "enabled": index % 2 == 0,
            "recorded_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "payload": f"row-{index}".encode(),
        }
        for index in range(1, 22)
    ]
    with source.begin() as connection:
        connection.execute(source_table.insert(), rows)
    with target.begin() as connection:
        changed = [dict(row) for row in rows]
        changed[20]["payload"] = b"changed-row-21"
        connection.execute(target_table.insert(), changed)
    with source.connect() as source_connection, target.connect() as target_connection:
        assert migration._table_digest(
            source_connection, source_table, canonical_table=target_table, chunk_size=3
        ) != migration._table_digest(target_connection, target_table, canonical_table=target_table, chunk_size=4)


def test_digest_normalization_handles_cross_driver_types() -> None:
    boolean = sa.Boolean()
    timestamp = sa.DateTime(timezone=True)
    binary = sa.LargeBinary()
    assert migration._normalize_digest_value(1, boolean) is True
    assert migration._normalize_digest_value(False, boolean) is False
    assert migration._normalize_digest_value("2026-01-01T08:00:00+08:00", timestamp) == "2026-01-01T00:00:00.000000Z"
    assert (
        migration._normalize_digest_value(datetime(2026, 1, 1, tzinfo=timezone.utc), timestamp)
        == "2026-01-01T00:00:00.000000Z"
    )
    assert migration._normalize_digest_value(b"secret", binary) == {"base64": "c2VjcmV0"}


def test_composite_foreign_key_verification_and_unsupported_metadata() -> None:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    parent = sa.Table(
        "parents",
        metadata,
        sa.Column("tenant_id", sa.String, primary_key=True),
        sa.Column("id", sa.Integer, primary_key=True),
    )
    child = sa.Table(
        "children",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.String),
        sa.Column("parent_id", sa.Integer),
        sa.ForeignKeyConstraint(["tenant_id", "parent_id"], ["parents.tenant_id", "parents.id"]),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(parent.insert(), {"tenant_id": "a", "id": 1})
        connection.execute(
            child.insert(),
            [
                {"id": 1, "tenant_id": "a", "parent_id": 1},
                {"id": 2, "tenant_id": "a", "parent_id": 2},
            ],
        )
    with engine.connect() as connection:
        fk = sa.inspect(connection).get_foreign_keys("children")[0]
        assert migration._missing_fk_count(connection, child, fk, metadata) == 1
        with pytest.raises(RuntimeError, match="Unsupported foreign key metadata"):
            migration._missing_fk_count(
                connection,
                child,
                {"constrained_columns": ["tenant_id", "parent_id"], "referred_columns": ["id"]},
                metadata,
            )


def test_self_foreign_key_verification_uses_alias() -> None:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    nodes = sa.Table(
        "nodes",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("parent_id", sa.Integer, sa.ForeignKey("nodes.id")),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(nodes.insert(), [{"id": 1, "parent_id": None}, {"id": 2, "parent_id": 99}])
    with engine.connect() as connection:
        fk = sa.inspect(connection).get_foreign_keys("nodes")[0]
        assert migration._missing_fk_count(connection, nodes, fk, metadata) == 1


def test_cli_invalid_chunk_size_fails_with_atomic_structured_report(tmp_path: Path, capsys) -> None:
    source_path = tmp_path / "source.db"
    source = sa.create_engine(f"sqlite:///{source_path.as_posix()}")
    metadata = sa.MetaData()
    sa.Table("users", metadata, sa.Column("id", sa.String, primary_key=True))
    sa.Table("projects", metadata, sa.Column("id", sa.String, primary_key=True))
    metadata.create_all(source)
    source.dispose()
    report_path = tmp_path / "report.json"

    exit_code = migration.main(
        [
            "--source",
            str(source_path),
            "--target",
            "postgresql://user:super-secret@localhost/db",
            "--chunk-size",
            "0",
            "--report",
            str(report_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "[fail]" in captured.err
    assert "[ok]" not in captured.out
    assert "super-secret" not in captured.out + captured.err + report_path.read_text(encoding="utf-8")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["partial"] is False
    assert report["verification"]["status"] == "failed"
    assert report["verification"]["failures"]


def test_table_digest_rejects_invalid_chunk_size() -> None:
    engine = sa.create_engine("sqlite://")
    table = _table(engine)
    with engine.connect() as connection, pytest.raises(ValueError, match="greater than zero"):
        migration._table_digest(connection, table, chunk_size=0)


def test_failure_message_masks_embedded_database_password() -> None:
    rendered = migration._sanitize_failure_message(
        "connection failed: postgresql://admin:top-secret@db.example/production"
    )
    assert "top-secret" not in rendered
    assert "postgresql://admin:***@db.example/production" in rendered


def test_database_statement_error_never_leaks_parameters(monkeypatch, capsys) -> None:
    secret = "statement-secret-marker"

    def _raise(_argv):  # type: ignore[no-untyped-def]
        raise sa.exc.StatementError("unsafe statement", "SELECT :secret", {"secret": secret}, ValueError())

    monkeypatch.setattr(migration, "_run_main", _raise)
    assert migration.main([]) == 1
    captured = capsys.readouterr()
    assert secret not in captured.err
    assert "SELECT" not in captured.err
    assert "Database operation failed (StatementError)" in captured.err


def test_partial_reflects_actual_inserted_rows() -> None:
    assert migration._report_has_inserted_rows({"tables": {"users": {"attempted": 2, "inserted": 0}}}) is False
    assert migration._report_has_inserted_rows({"tables": {"users": {"attempted": 2, "inserted": 1}}}) is True


def test_required_postgres_extensions_include_vector() -> None:
    class _Rows:
        def fetchall(self):  # type: ignore[no-untyped-def]
            return [("uuid-ossp",), ("pg_trgm",)]

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

        def execute(self, _statement):  # type: ignore[no-untyped-def]
            return _Rows()

    class _Engine:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        def connect(self) -> _Connection:
            return _Connection()

    assert migration._pg_required_extensions(_Engine()) == {  # type: ignore[arg-type]
        "uuid-ossp": True,
        "pg_trgm": True,
        "vector": False,
    }
    assert migration._missing_required_extensions({"uuid-ossp": True, "pg_trgm": True, "vector": False}) == ["vector"]


def test_failure_report_write_error_does_not_mask_original_failure(monkeypatch, capsys, tmp_path: Path) -> None:
    def _fail(_argv):  # type: ignore[no-untyped-def]
        migration._ACTIVE_REPORT = {"tables": {}, "verification": {"status": "pending", "failures": []}}
        migration._ACTIVE_REPORT_PATH = str(tmp_path / "report.json")
        raise RuntimeError("original failure")

    monkeypatch.setattr(migration, "_run_main", _fail)
    monkeypatch.setattr(migration, "_write_report", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))
    assert migration.main([]) == 1
    captured = capsys.readouterr()
    assert "original failure" in captured.err
    assert "Unable to persist failure report" in captured.err


def test_postgres_copy_and_verification_transactions_force_utc() -> None:
    source = inspect.getsource(migration)
    assert source.count("SET LOCAL TIME ZONE 'UTC'") == 3
