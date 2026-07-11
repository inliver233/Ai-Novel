from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command

from app.db import migrations


PREVIOUS_REVISION = "b2d4e6f8a0c1"
RECONCILIATION_REVISION = "c7e2a4f6b8d0"
HEAD_REVISION = "b6d1e8f3a5c7"
_NOW = "2026-07-11 00:00:00+00:00"
_USER_ID = "u" * 36


def _run_alembic(database_url: str, revision: str, *, downgrade: bool = False) -> None:
    config = migrations._alembic_config(database_url=database_url)
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        if downgrade:
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def _run_alembic_check(database_url: str) -> None:
    config = migrations._alembic_config(database_url=database_url)
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.check(config)
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def _revision(engine: sa.Engine) -> str:
    with engine.connect() as connection:
        return str(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one())


def _column(engine: sa.Engine, table_name: str, column_name: str) -> dict[str, Any]:
    return next(column for column in sa.inspect(engine).get_columns(table_name) if column["name"] == column_name)


def _index_names(engine: sa.Engine, table_name: str) -> set[str]:
    return {str(index["name"]) for index in sa.inspect(engine).get_indexes(table_name)}


def _seed_previous_revision(engine: sa.Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (id, created_at, updated_at) VALUES (?, ?, ?)",
            (_USER_ID, _NOW, _NOW),
        )
        connection.exec_driver_sql(
            """
            INSERT INTO projects (id, owner_user_id, name, created_at, updated_at)
            VALUES ('project-1', ?, 'Project', ?, ?)
            """,
            (_USER_ID, _NOW, _NOW),
        )
        connection.exec_driver_sql(
            """
            INSERT INTO outlines (id, project_id, title, created_at, updated_at)
            VALUES ('outline-1', 'project-1', 'Outline', ?, ?)
            """,
            (_NOW, _NOW),
        )
        connection.exec_driver_sql("UPDATE projects SET active_outline_id = 'outline-1' WHERE id = 'project-1'")
        connection.exec_driver_sql(
            """
            INSERT INTO llm_presets (project_id, provider, model)
            VALUES ('project-1', 'openai', 'gpt-4o-mini')
            """
        )
        connection.exec_driver_sql("INSERT INTO project_settings (project_id) VALUES ('project-1')")
        connection.exec_driver_sql(
            """
            INSERT INTO batch_generation_tasks (
                id, project_id, outline_id, actor_user_id, created_at, updated_at
            ) VALUES ('batch-1', 'project-1', 'outline-1', ?, ?, ?)
            """,
            (_USER_ID, _NOW, _NOW),
        )
        connection.exec_driver_sql(
            """
            INSERT INTO batch_generation_task_items (
                id, task_id, chapter_number, created_at, updated_at
            ) VALUES ('batch-item-1', 'batch-1', 1, ?, ?)
            """,
            (_NOW, _NOW),
        )
        connection.exec_driver_sql(
            """
            INSERT INTO vector_rag_profiles (id, owner_user_id, name, created_at, updated_at)
            VALUES ('rag-profile-1', ?, 'RAG', ?, ?)
            """,
            (_USER_ID, _NOW, _NOW),
        )
        connection.exec_driver_sql(
            """
            INSERT INTO writing_styles (
                id, owner_user_id, name, prompt_content, is_preset, created_at, updated_at
            ) VALUES ('style-1', ?, 'Style', 'Prompt', 0, ?, ?)
            """,
            (_USER_ID, _NOW, _NOW),
        )
        connection.exec_driver_sql(
            """
            INSERT INTO llm_profiles (
                id, owner_user_id, name, provider, model, created_at, updated_at
            ) VALUES ('llm-profile-1', ?, 'LLM', 'openai', 'model', ?, ?)
            """,
            (_USER_ID, _NOW, _NOW),
        )
        connection.exec_driver_sql(
            """
            INSERT INTO llm_task_presets (
                project_id, task_key, llm_profile_id, provider, model, created_at, updated_at
            ) VALUES ('project-1', 'chapter', 'llm-profile-1', 'openai', 'model', ?, ?)
            """,
            (_NOW, _NOW),
        )
        connection.exec_driver_sql(
            """
            INSERT INTO detailed_outlines (
                id, outline_id, project_id, volume_number, volume_title, created_at, updated_at
            ) VALUES ('detail-1', 'outline-1', 'project-1', 1, 'Volume', ?, ?)
            """,
            (_NOW, _NOW),
        )
        connection.exec_driver_sql(
            """
            INSERT INTO project_tasks (
                id, project_id, actor_user_id, kind, status, idempotency_key, created_at, updated_at
            ) VALUES ('task-1', 'project-1', ?, 'noop', 'queued', 'key-1', ?, ?)
            """,
            (_USER_ID, _NOW, _NOW),
        )
        connection.exec_driver_sql(
            """
            INSERT INTO user_usage_stats (user_id, created_at, updated_at)
            VALUES (?, ?, ?)
            """,
            (_USER_ID, _NOW, _NOW),
        )


def _data_snapshot(engine: sa.Engine) -> dict[str, tuple[Any, ...]]:
    queries = {
        "batch": """
            SELECT actor_user_id, status, total_count, completed_count, failed_count,
                   skipped_count, cancel_requested, pause_requested
            FROM batch_generation_tasks WHERE id = 'batch-1'
        """,
        "batch_item": """
            SELECT status, attempt_count FROM batch_generation_task_items
            WHERE id = 'batch-item-1'
        """,
        "rag": "SELECT owner_user_id, name FROM vector_rag_profiles WHERE id = 'rag-profile-1'",
        "style": "SELECT owner_user_id, name FROM writing_styles WHERE id = 'style-1'",
        "settings": """
            SELECT context_optimizer_enabled, auto_update_worldbook_enabled,
                   auto_update_characters_enabled, auto_update_story_memory_enabled,
                   auto_update_graph_enabled, auto_update_vector_enabled,
                   auto_update_search_enabled, auto_update_fractal_enabled,
                   auto_update_tables_enabled, vector_index_dirty
            FROM project_settings WHERE project_id = 'project-1'
        """,
        "user": "SELECT is_admin FROM users WHERE id = '" + _USER_ID + "'",
        "usage": f"""
            SELECT total_generation_calls, total_generation_error_calls, total_generated_chars
            FROM user_usage_stats WHERE user_id = '{_USER_ID}'
        """,
        "detail": "SELECT status FROM detailed_outlines WHERE id = 'detail-1'",
        "task": "SELECT attempt FROM project_tasks WHERE id = 'task-1'",
        "preset": """
            SELECT project_id, task_key, llm_profile_id FROM llm_task_presets
            WHERE project_id = 'project-1' AND task_key = 'chapter'
        """,
        "project": "SELECT active_outline_id FROM projects WHERE id = 'project-1'",
    }
    with engine.connect() as connection:
        snapshot: dict[str, tuple[Any, ...]] = {}
        for name, query in queries.items():
            row = connection.exec_driver_sql(query).one_or_none()
            assert row is not None, f"missing seeded row for {name}"
            snapshot[name] = tuple(row)
        return snapshot


def test_reconciliation_round_trip_preserves_data_defaults_and_indexes(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'reconciliation.db').as_posix()}"
    _run_alembic(database_url, PREVIOUS_REVISION)
    engine = sa.create_engine(database_url)
    try:
        _seed_previous_revision(engine)
        expected_data = _data_snapshot(engine)

        _run_alembic(database_url, RECONCILIATION_REVISION)

        assert _revision(engine) == RECONCILIATION_REVISION
        for table_name, column_name in (
            ("batch_generation_tasks", "actor_user_id"),
            ("vector_rag_profiles", "owner_user_id"),
            ("writing_styles", "owner_user_id"),
        ):
            assert isinstance(_column(engine, table_name, column_name)["type"], sa.String)
            assert _column(engine, table_name, column_name)["type"].length == 64
        assert "ix_projects_active_outline_id" in _index_names(engine, "projects")
        assert {
            "ix_llm_task_presets_project_id",
            "ix_llm_task_presets_llm_profile_id",
        }.issubset(_index_names(engine, "llm_task_presets"))
        assert _data_snapshot(engine) == expected_data
        _run_alembic(database_url, PREVIOUS_REVISION, downgrade=True)

        assert _revision(engine) == PREVIOUS_REVISION
        for table_name, column_name in (
            ("batch_generation_tasks", "actor_user_id"),
            ("vector_rag_profiles", "owner_user_id"),
            ("writing_styles", "owner_user_id"),
        ):
            assert _column(engine, table_name, column_name)["type"].length == 36
        assert _data_snapshot(engine) == expected_data

        _run_alembic(database_url, "head")
        assert _revision(engine) == HEAD_REVISION
        assert _data_snapshot(engine) == expected_data
        _run_alembic_check(database_url)
    finally:
        engine.dispose()


def test_reconciliation_downgrade_refuses_oversized_user_ids(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'oversized-user-id.db').as_posix()}"
    _run_alembic(database_url, RECONCILIATION_REVISION)
    engine = sa.create_engine(database_url)
    long_user_id = "user-" + "x" * 40
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO users (id, created_at, updated_at) VALUES (?, ?, ?)",
                (long_user_id, _NOW, _NOW),
            )
            connection.exec_driver_sql(
                """
                INSERT INTO writing_styles (
                    id, owner_user_id, name, prompt_content, is_preset, created_at, updated_at
                ) VALUES ('long-style', ?, 'Long', 'Prompt', 0, ?, ?)
                """,
                (long_user_id, _NOW, _NOW),
            )

        with pytest.raises(RuntimeError, match=r"writing_styles\.owner_user_id=length 45"):
            _run_alembic(database_url, PREVIOUS_REVISION, downgrade=True)

        assert _revision(engine) == RECONCILIATION_REVISION
        assert _column(engine, "writing_styles", "owner_user_id")["type"].length == 64
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT owner_user_id FROM writing_styles WHERE id = 'long-style'"
                ).scalar_one()
                == long_user_id
            )
    finally:
        engine.dispose()
