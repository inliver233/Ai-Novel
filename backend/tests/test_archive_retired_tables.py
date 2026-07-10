from __future__ import annotations

import importlib.util
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic import command

from app.db import migrations


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "archive_retired_tables.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_archive_retired_tables", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module() -> ModuleType:
    return _load_script()


def _database(tmp_path: Path, module: ModuleType) -> sa.Engine:
    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'retired.db').as_posix()}")
    metadata = sa.MetaData()
    entities = sa.Table(
        "entities",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("payload", sa.Text(), nullable=True),
    )
    sa.Table(
        "relations",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("entity_id", sa.String(36), sa.ForeignKey(entities.c.id), nullable=False),
    )
    change_sets = sa.Table(
        "memory_change_sets",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
    )
    sa.Table(
        "memory_change_set_items",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_set_id", sa.String(36), sa.ForeignKey(change_sets.c.id), nullable=False),
    )
    sa.Table(
        "memory_tasks",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("change_set_id", sa.String(36), sa.ForeignKey(change_sets.c.id), nullable=False),
    )
    project_tables = sa.Table(
        "project_tables",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
    )
    sa.Table(
        "project_table_rows",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("table_id", sa.String(36), sa.ForeignKey(project_tables.c.id), nullable=False),
    )
    defined = set(metadata.tables)
    for name in module.RETIRED_TABLES:
        if name not in defined:
            sa.Table(name, metadata, sa.Column("id", sa.String(36), primary_key=True))
    sa.Table("active_data", metadata, sa.Column("id", sa.String(36), primary_key=True))
    metadata.create_all(engine)
    return engine


def _seed_every_retired_table(engine: sa.Engine, module: ModuleType) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO entities (id, payload) VALUES ('entity-1', '雪')"))
        conn.execute(sa.text("INSERT INTO relations (id, entity_id) VALUES ('relation-1', 'entity-1')"))
        conn.execute(sa.text("INSERT INTO memory_change_sets (id) VALUES ('change-set-1')"))
        conn.execute(
            sa.text("INSERT INTO memory_change_set_items (id, change_set_id) VALUES ('change-item-1', 'change-set-1')")
        )
        conn.execute(sa.text("INSERT INTO memory_tasks (id, change_set_id) VALUES ('memory-task-1', 'change-set-1')"))
        conn.execute(sa.text("INSERT INTO project_tables (id) VALUES ('project-table-1')"))
        conn.execute(
            sa.text("INSERT INTO project_table_rows (id, table_id) VALUES ('project-row-1', 'project-table-1')")
        )
        seeded = {
            "entities",
            "relations",
            "memory_change_sets",
            "memory_change_set_items",
            "memory_tasks",
            "project_tables",
            "project_table_rows",
        }
        for name in module.RETIRED_TABLES:
            if name not in seeded:
                conn.execute(sa.text(f'INSERT INTO "{name}" (id) VALUES (:id)'), {"id": f"{name}-1"})
        conn.execute(sa.text("INSERT INTO active_data (id) VALUES ('keep-me')"))


def _counts(engine: sa.Engine, names: tuple[str, ...]) -> dict[str, int]:
    with engine.connect() as conn:
        return {name: int(conn.execute(sa.text(f'SELECT COUNT(*) FROM "{name}"')).scalar_one()) for name in names}


def _archive_files(path: Path) -> dict[str, bytes]:
    return {file.relative_to(path).as_posix(): file.read_bytes() for file in sorted(path.rglob("*")) if file.is_file()}


def _upgrade_with_alembic(database_url: str, revision: str) -> None:
    cfg = migrations._alembic_config(database_url=database_url)
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(cfg, revision)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def _seed_real_schema(engine: sa.Engine) -> None:
    timestamp = "2026-02-03 04:05:06.123456"
    statements = [
        (
            "INSERT INTO users (id,email,display_name,created_at,updated_at,is_admin) "
            "VALUES ('user-1','archive@example.test','Archive',:ts,:ts,0)",
            {"ts": timestamp},
        ),
        (
            "INSERT INTO projects (id,owner_user_id,name,created_at,updated_at) "
            "VALUES ('project-1','user-1','Project',:ts,:ts)",
            {"ts": timestamp},
        ),
        (
            "INSERT INTO outlines (id,project_id,title,created_at,updated_at) "
            "VALUES ('outline-1','project-1','Outline',:ts,:ts)",
            {"ts": timestamp},
        ),
        (
            "INSERT INTO chapters (id,project_id,number,title,status,outline_id,updated_at) "
            "VALUES ('chapter-1','project-1',1,'Chapter','draft','outline-1',:ts)",
            {"ts": timestamp},
        ),
        (
            "INSERT INTO entities "
            "(id,project_id,entity_type,name,attributes_json,created_at,updated_at) "
            "VALUES ('entity-1','project-1','character','雪',:attributes,:ts,:ts)",
            {"attributes": '{"age":18}', "ts": timestamp},
        ),
        (
            "INSERT INTO worldbook_entries "
            "(id,project_id,title,content_md,enabled,constant,keywords_json,exclude_recursion,"
            "prevent_recursion,char_limit,priority,updated_at) VALUES "
            "('worldbook-1','project-1','World','Lore',1,0,'[\"雪\"]',1,0,2048,'high',:ts)",
            {"ts": timestamp},
        ),
        (
            "INSERT INTO plot_analysis "
            "(id,project_id,chapter_id,analysis_json,overall_quality_score,coherence_score,created_at) "
            "VALUES ('plot-1','project-1','chapter-1',:analysis,0.75,0.5,:ts)",
            {"analysis": '{"ok":true}', "ts": timestamp},
        ),
    ]
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.commit()
        with conn.begin():
            for statement, params in statements:
                conn.execute(sa.text(statement), params)


def test_exact_retired_table_set_is_stable(module: ModuleType) -> None:
    assert module.RETIRED_TABLES == (
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


def test_tool_rejects_non_sqlite_dialects(module: ModuleType) -> None:
    engine = sa.create_mock_engine("postgresql://", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="SQLite only.*pg_dump"):
        module.preflight(engine)


def test_preflight_reports_all_empty_and_missing_tables_deterministically(tmp_path: Path, module: ModuleType) -> None:
    engine = _database(tmp_path, module)
    try:
        first = module.preflight(engine)
        second = module.preflight(engine)
        with engine.begin() as conn:
            conn.execute(sa.text("DROP TABLE fractal_memory"))
        missing = module.preflight(engine)
    finally:
        engine.dispose()

    assert first == second
    assert first["retired_table_count"] == 14
    assert first["present_table_count"] == 14
    assert first["missing_table_count"] == 0
    assert first["total_rows"] == 0
    assert [table["name"] for table in first["tables"]] == list(module.RETIRED_TABLES)
    assert all(table["row_count"] == 0 and len(table["sha256"]) == 64 for table in first["tables"])
    assert missing["present_table_count"] == 13
    assert missing["missing_table_count"] == 1
    assert next(table for table in missing["tables"] if table["name"] == "fractal_memory")["present"] is False


def test_archive_is_canonical_deterministic_and_checksummed(tmp_path: Path, module: ModuleType) -> None:
    engine = _database(tmp_path, module)
    _seed_every_retired_table(engine, module)
    first_dir = tmp_path / "archive-1"
    second_dir = tmp_path / "archive-2"
    try:
        first = module.archive(engine, first_dir)
        second = module.archive(engine, second_dir)
    finally:
        engine.dispose()

    assert first["total_rows"] == 14
    assert second["archive_content_sha256"] == first["archive_content_sha256"]
    assert _archive_files(first_dir) == _archive_files(second_dir)

    manifest = json.loads((first_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["retired_table_count"] == 14
    assert manifest["total_rows"] == 14
    assert [entry["name"] for entry in manifest["tables"]] == list(module.RETIRED_TABLES)
    assert all(entry["row_count"] == 1 for entry in manifest["tables"])
    assert (first_dir / "tables" / "entities.json").read_bytes().endswith(b"\n")


def test_purge_requires_confirmation_then_deletes_in_fk_safe_transaction(tmp_path: Path, module: ModuleType) -> None:
    engine = _database(tmp_path, module)
    _seed_every_retired_table(engine, module)
    archive_dir = tmp_path / "archive"
    module.archive(engine, archive_dir)

    try:
        with pytest.raises(PermissionError, match=module.PURGE_CONFIRMATION):
            module.purge(engine, archive_dir, confirmation=None)
        assert set(_counts(engine, module.RETIRED_TABLES).values()) == {1}

        result = module.purge(engine, archive_dir, confirmation=module.PURGE_CONFIRMATION)
        assert result["total_deleted_rows"] == 14
        assert set(result["deleted_rows"]) == set(module.RETIRED_TABLES)
        assert set(_counts(engine, module.RETIRED_TABLES).values()) == {0}
        assert _counts(engine, ("active_data",)) == {"active_data": 1}

        restored = module.restore(engine, archive_dir)
        assert restored["total_restored_rows"] == 14
        assert set(_counts(engine, module.RETIRED_TABLES).values()) == {1}
        assert _counts(engine, ("active_data",)) == {"active_data": 1}
    finally:
        engine.dispose()


def test_purge_rejects_corrupt_or_stale_archive_without_deleting(tmp_path: Path, module: ModuleType) -> None:
    engine = _database(tmp_path, module)
    _seed_every_retired_table(engine, module)
    corrupt_dir = tmp_path / "corrupt"
    stale_dir = tmp_path / "stale"
    module.archive(engine, corrupt_dir)
    module.archive(engine, stale_dir)
    (corrupt_dir / "tables" / "events.json").write_text("{}\n", encoding="utf-8")

    try:
        with pytest.raises(module.ArchiveVerificationError, match="canonical JSON|checksum mismatch"):
            module.purge(engine, corrupt_dir, confirmation=module.PURGE_CONFIRMATION)
        assert set(_counts(engine, module.RETIRED_TABLES).values()) == {1}

        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO events (id) VALUES ('events-2')"))
        before = _counts(engine, module.RETIRED_TABLES)
        with pytest.raises(module.ArchiveVerificationError, match="changed after the archive"):
            module.purge(engine, stale_dir, confirmation=module.PURGE_CONFIRMATION)
        assert _counts(engine, module.RETIRED_TABLES) == before
    finally:
        engine.dispose()


def test_purge_rolls_back_earlier_child_deletes_when_later_delete_fails(tmp_path: Path, module: ModuleType) -> None:
    engine = _database(tmp_path, module)
    _seed_every_retired_table(engine, module)
    archive_dir = tmp_path / "archive"
    module.archive(engine, archive_dir)
    before = _counts(engine, module.RETIRED_TABLES)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TRIGGER fail_entities_delete BEFORE DELETE ON entities "
                "BEGIN SELECT RAISE(ABORT, 'injected purge failure'); END"
            )
        )

    try:
        with pytest.raises(sa.exc.IntegrityError, match="injected purge failure"):
            module.purge(engine, archive_dir, confirmation=module.PURGE_CONFIRMATION)
        assert _counts(engine, module.RETIRED_TABLES) == before
    finally:
        engine.dispose()


def test_restore_requires_empty_exactly_compatible_tables(tmp_path: Path, module: ModuleType) -> None:
    engine = _database(tmp_path, module)
    _seed_every_retired_table(engine, module)
    archive_dir = tmp_path / "archive"
    module.archive(engine, archive_dir)
    try:
        with pytest.raises(module.ArchiveVerificationError, match="is not empty"):
            module.restore(engine, archive_dir)

        module.purge(engine, archive_dir, confirmation=module.PURGE_CONFIRMATION)
        with engine.begin() as conn:
            conn.execute(sa.text("ALTER TABLE events ADD COLUMN unexpected TEXT"))
        with pytest.raises(module.ArchiveVerificationError, match="incompatible with the archive"):
            module.restore(engine, archive_dir)
        assert set(_counts(engine, module.RETIRED_TABLES).values()) == {0}
    finally:
        engine.dispose()


def test_purge_holds_sqlite_writer_lock_between_verification_and_delete(tmp_path: Path, module: ModuleType) -> None:
    engine = _database(tmp_path, module)
    _seed_every_retired_table(engine, module)
    archive_dir = tmp_path / "archive"
    module.archive(engine, archive_dir)
    contender = sa.create_engine(engine.url, connect_args={"timeout": 0.05})

    def attempt_unarchived_write() -> BaseException | None:
        try:
            with contender.begin() as conn:
                conn.execute(sa.text("INSERT INTO events (id) VALUES ('unarchived-event')"))
        except BaseException as exc:
            return exc
        return None

    def after_verify() -> None:
        with ThreadPoolExecutor(max_workers=1) as executor:
            error = executor.submit(attempt_unarchived_write).result(timeout=2)
        assert isinstance(error, sa.exc.OperationalError)
        assert "database is locked" in str(error).lower()

    try:
        result = module.purge(
            engine,
            archive_dir,
            confirmation=module.PURGE_CONFIRMATION,
            _after_verify=after_verify,
        )
        assert result["total_deleted_rows"] == 14
        assert set(_counts(engine, module.RETIRED_TABLES).values()) == {0}
    finally:
        contender.dispose()
        engine.dispose()


@pytest.mark.parametrize("revision", ["ee23bd2770fc", "head"])
def test_alembic_legacy_and_head_schema_typed_archive_round_trip(
    tmp_path: Path,
    module: ModuleType,
    revision: str,
) -> None:
    database_path = tmp_path / f"alembic-{revision}.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    _upgrade_with_alembic(database_url, revision)
    engine = sa.create_engine(database_url)
    _seed_real_schema(engine)
    first_dir = tmp_path / f"archive-{revision}"
    second_dir = tmp_path / f"restored-{revision}"
    try:
        archived = module.archive(engine, first_dir)
        assert archived["total_rows"] == 3
        entities_payload = json.loads((first_dir / "tables" / "entities.json").read_text(encoding="utf-8"))
        worldbook_payload = json.loads((first_dir / "tables" / "worldbook_entries.json").read_text(encoding="utf-8"))
        assert entities_payload["rows"][0]["created_at"]["$type"] == "datetime"
        assert worldbook_payload["rows"][0]["enabled"] is True

        purged = module.purge(engine, first_dir, confirmation=module.PURGE_CONFIRMATION)
        assert purged["total_deleted_rows"] == 3
        restored = module.restore(engine, first_dir)
        assert restored["total_restored_rows"] == 3
        module.archive(engine, second_dir)
        assert _archive_files(first_dir) == _archive_files(second_dir)
    finally:
        engine.dispose()
