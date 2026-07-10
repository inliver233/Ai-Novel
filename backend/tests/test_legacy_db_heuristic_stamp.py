"""backend-data#P3: 遗留 SQLite 库不得被启发式误盖 Alembic head 戳。

``ensure_db_schema`` 曾只凭 ``outlines``、``llm_profiles`` 两张表与
``projects.active_outline_id`` / ``projects.llm_profile_id`` 两列，就把任何
无 ``alembic_version`` 的 SQLite 库盖为当前 head。上述结构实际早在
``1c2a0e6b4c2d`` 就已出现；直接盖 head 会跳过其后的所有真实迁移，使
``entries``、``detailed_outlines``、``vector_rag_profiles`` 等后续表永久缺失。

测试用 Alembic 真实升级到 ``1c2a0e6b4c2d`` 来生成中间态 schema，再移除
``alembic_version``，避免用无法对应任何历史 revision 的骨架库制造假场景。
正确实现必须识别该真实 revision、从那里升级到动态解析出的当前 head，并保留
已有用户、项目、大纲与 LLM profile 数据。对于无法安全映射到已知 revision 的
非空 partial schema，则必须 fail closed，给出人工处理指引且不得写入版本戳。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from app.core.config import settings
from app.db import migrations


LEGACY_INTERMEDIATE_REVISION = "1c2a0e6b4c2d"
_SEEDED_AT = "2026-01-02T03:04:05Z"


def _database_url(db_path: Path) -> str:
    return f"sqlite:///{db_path.as_posix()}"


def _current_head(database_url: str) -> str:
    cfg = migrations._alembic_config(database_url=database_url)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    assert head is not None
    return head


def _build_authentic_legacy_intermediate_db(db_path: Path, database_url: str) -> None:
    """生成真实 1c2a0e6b4c2d schema，种入数据后模拟版本表遗失。"""

    cfg = migrations._alembic_config(database_url=database_url)
    command.upgrade(cfg, LEGACY_INTERMEDIATE_REVISION)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            INSERT INTO users (
                id, email, password_hash, display_name, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("legacy-user", "legacy@example.test", "hash", "Legacy User", _SEEDED_AT, _SEEDED_AT),
        )
        conn.execute(
            """
            INSERT INTO projects (
                id, owner_user_id, name, genre, logline, created_at, updated_at,
                active_outline_id, llm_profile_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-project",
                "legacy-user",
                "Legacy Project",
                "fantasy",
                "A preserved legacy project",
                _SEEDED_AT,
                _SEEDED_AT,
                None,
                None,
            ),
        )
        conn.execute(
            """
            INSERT INTO outlines (
                id, project_id, title, content_md, structure_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-outline",
                "legacy-project",
                "Legacy Outline",
                "Preserve this outline",
                '{"version":1}',
                _SEEDED_AT,
                _SEEDED_AT,
            ),
        )
        conn.execute(
            """
            INSERT INTO llm_profiles (
                id, owner_user_id, name, provider, base_url, model, api_key,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-profile",
                "legacy-user",
                "Legacy Profile",
                "mock",
                None,
                "legacy-model",
                "legacy-api-key",
                _SEEDED_AT,
                _SEEDED_AT,
            ),
        )
        conn.execute(
            """
            UPDATE projects
            SET active_outline_id = ?, llm_profile_id = ?
            WHERE id = ?
            """,
            ("legacy-outline", "legacy-profile", "legacy-project"),
        )
        conn.execute("DROP TABLE alembic_version")
        conn.commit()
    finally:
        conn.close()


def _build_authentic_init_db(db_path: Path, database_url: str) -> None:
    """生成真实 INIT schema，种入旧单大纲数据后模拟版本表遗失。"""

    cfg = migrations._alembic_config(database_url=database_url)
    command.upgrade(cfg, migrations.INIT_REVISION)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            INSERT INTO users (
                id, email, password_hash, display_name, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("init-user", "init@example.test", "hash", "Init User", _SEEDED_AT, _SEEDED_AT),
        )
        conn.execute(
            """
            INSERT INTO projects (
                id, owner_user_id, name, genre, logline, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "init-project",
                "init-user",
                "Init Project",
                "mystery",
                "An INIT-era project",
                _SEEDED_AT,
                _SEEDED_AT,
            ),
        )
        conn.execute(
            """
            INSERT INTO outline (project_id, content_md, updated_at)
            VALUES (?, ?, ?)
            """,
            ("init-project", "Preserve the INIT outline", _SEEDED_AT),
        )
        conn.execute("DROP TABLE alembic_version")
        conn.commit()
    finally:
        conn.close()


def _build_unrecognized_partial_db(db_path: Path) -> None:
    """生成不对应任何完整历史 revision、但会命中旧启发式的骨架 schema。"""

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE projects (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                active_outline_id VARCHAR(36),
                llm_profile_id VARCHAR(36)
            )
            """
        )
        conn.execute("CREATE TABLE outlines (id VARCHAR(36) NOT NULL PRIMARY KEY)")
        conn.execute("CREATE TABLE llm_profiles (id VARCHAR(36) NOT NULL PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()


def _configure_sqlite_test(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_url: str,
) -> None:
    # ensure_db_schema 从 settings 构建 cfg；alembic/env.py 又优先读环境变量。
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setattr(settings, "database_url", database_url)
    monkeypatch.setattr(settings, "app_env", "dev")


def test_ensure_db_schema_creates_missing_head_tables_for_legacy_db(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-intermediate.db"
    database_url = _database_url(db_path)
    _configure_sqlite_test(monkeypatch, database_url=database_url)
    _build_authentic_legacy_intermediate_db(db_path, database_url)
    expected_head = _current_head(database_url)

    engine = create_engine(database_url)
    try:
        migrations.ensure_db_schema(engine=engine)
    finally:
        engine.dispose()

    conn = sqlite3.connect(str(db_path))
    try:
        actual_head_row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        user_row = conn.execute(
            "SELECT email, display_name FROM users WHERE id = ?",
            ("legacy-user",),
        ).fetchone()
        project_row = conn.execute(
            """
            SELECT name, active_outline_id, llm_profile_id
            FROM projects WHERE id = ?
            """,
            ("legacy-project",),
        ).fetchone()
        outline_row = conn.execute(
            "SELECT title, content_md, structure_json FROM outlines WHERE id = ?",
            ("legacy-outline",),
        ).fetchone()
        profile_row = conn.execute(
            "SELECT name, provider, model FROM llm_profiles WHERE id = ?",
            ("legacy-profile",),
        ).fetchone()
    finally:
        conn.close()

    inspect_engine = create_engine(database_url)
    try:
        tables = set(inspect(inspect_engine).get_table_names())
    finally:
        inspect_engine.dispose()

    assert actual_head_row == (expected_head,)
    assert {
        "entries",
        "detailed_outlines",
        "user_usage_stats",
        "vector_rag_profiles",
    }.issubset(tables)
    assert user_row == ("legacy@example.test", "Legacy User")
    assert project_row == ("Legacy Project", "legacy-outline", "legacy-profile")
    assert outline_row == ("Legacy Outline", "Preserve this outline", '{"version":1}')
    assert profile_row == ("Legacy Profile", "mock", "legacy-model")


def test_ensure_db_schema_upgrades_authentic_init_legacy_db(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-init.db"
    database_url = _database_url(db_path)
    _configure_sqlite_test(monkeypatch, database_url=database_url)
    _build_authentic_init_db(db_path, database_url)
    expected_head = _current_head(database_url)

    engine = create_engine(database_url)
    try:
        migrations.ensure_db_schema(engine=engine)
    finally:
        engine.dispose()

    conn = sqlite3.connect(str(db_path))
    try:
        actual_head_row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        project_row = conn.execute(
            "SELECT name, active_outline_id FROM projects WHERE id = ?",
            ("init-project",),
        ).fetchone()
        assert project_row is not None
        active_outline_id = project_row[1]
        outline_row = conn.execute(
            "SELECT project_id, content_md FROM outlines WHERE id = ?",
            (active_outline_id,),
        ).fetchone()
        user_row = conn.execute(
            "SELECT email, display_name FROM users WHERE id = ?",
            ("init-user",),
        ).fetchone()
        has_vector_rag_profiles = bool(
            conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'vector_rag_profiles'
                """
            ).fetchone()
        )
    finally:
        conn.close()

    assert actual_head_row == (expected_head,)
    assert project_row[0] == "Init Project"
    assert isinstance(active_outline_id, str) and active_outline_id
    assert outline_row == ("init-project", "Preserve the INIT outline")
    assert user_row == ("init@example.test", "Init User")
    assert has_vector_rag_profiles is True


def test_ensure_db_schema_rejects_unrecognized_partial_sqlite_without_stamping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "unrecognized-partial.db"
    database_url = _database_url(db_path)
    _build_unrecognized_partial_db(db_path)
    _configure_sqlite_test(monkeypatch, database_url=database_url)

    engine = create_engine(database_url)
    try:
        with pytest.raises(RuntimeError) as exc_info:
            migrations.ensure_db_schema(engine=engine)
    finally:
        engine.dispose()

    message = str(exc_info.value).lower()
    assert "sqlite" in message
    assert "unversioned" in message or "alembic_version" in message
    assert "manual" in message or "backup" in message

    inspect_engine = create_engine(database_url)
    try:
        tables = set(inspect(inspect_engine).get_table_names())
    finally:
        inspect_engine.dispose()

    assert tables == {"projects", "outlines", "llm_profiles"}
    assert "alembic_version" not in tables


def test_ensure_db_schema_rejects_matching_tables_and_columns_with_missing_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "malformed-init.db"
    database_url = _database_url(db_path)
    _configure_sqlite_test(monkeypatch, database_url=database_url)
    _build_authentic_init_db(db_path, database_url)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DROP INDEX ix_projects_owner_user_id")
        conn.commit()
    finally:
        conn.close()

    engine = create_engine(database_url)
    try:
        with pytest.raises(RuntimeError, match="does not exactly match"):
            migrations.ensure_db_schema(engine=engine)
    finally:
        engine.dispose()

    inspect_engine = create_engine(database_url)
    try:
        inspector = inspect(inspect_engine)
        tables = set(inspector.get_table_names())
        project_indexes = {index["name"] for index in inspector.get_indexes("projects")}
    finally:
        inspect_engine.dispose()

    assert "alembic_version" not in tables
    assert "ix_projects_owner_user_id" not in project_indexes
