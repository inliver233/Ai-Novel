from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from app.db import migrations


PREVIOUS_REVISION = "d8f3b5a7c9e1"
QUOTA_REVISION = "f3a1c7e9b2d4"
_NOW = "2026-07-11 00:00:00+00:00"


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


def _seed_project(connection: sa.Connection, *, project_id: str, user_id: str, status: str, task_id: str) -> None:
    connection.exec_driver_sql(
        "INSERT OR IGNORE INTO users (id, created_at, updated_at) VALUES (?, ?, ?)",
        (user_id, _NOW, _NOW),
    )
    connection.exec_driver_sql(
        """
        INSERT INTO projects (id, owner_user_id, name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (project_id, user_id, project_id, _NOW, _NOW),
    )
    outline_id = f"outline-{project_id}"
    connection.exec_driver_sql(
        """
        INSERT INTO outlines (id, project_id, title, created_at, updated_at)
        VALUES (?, ?, 'Outline', ?, ?)
        """,
        (outline_id, project_id, _NOW, _NOW),
    )
    connection.exec_driver_sql(
        """
        INSERT INTO batch_generation_tasks (
            id, project_id, outline_id, actor_user_id, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (task_id, project_id, outline_id, user_id, status, _NOW, _NOW),
    )


def test_quota_migration_backfills_authoritative_provider_and_downgrades(tmp_path: Path) -> None:
    database_path = tmp_path / "quota-migration.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    _run_alembic(database_url, PREVIOUS_REVISION)
    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            _seed_project(connection, project_id="p-task", user_id="u-task", status="queued", task_id="task-override")
            _seed_project(
                connection, project_id="p-default", user_id="u-default", status="paused", task_id="task-default"
            )
            _seed_project(
                connection, project_id="p-terminal", user_id="u-terminal", status="succeeded", task_id="task-terminal"
            )
            connection.exec_driver_sql(
                "INSERT INTO llm_presets (project_id, provider, model) VALUES ('p-task', 'openai', 'gpt-4o-mini')"
            )
            connection.exec_driver_sql(
                "INSERT INTO llm_presets (project_id, provider, model) VALUES ('p-default', ' google ', 'gemini-2.0-flash')"
            )
            connection.exec_driver_sql(
                """
                INSERT INTO llm_task_presets (
                    project_id, task_key, provider, model, created_at, updated_at
                ) VALUES ('p-task', 'chapter_generate', ' anthropic ', 'claude-3-5-sonnet-latest', ?, ?)
                """,
                (_NOW, _NOW),
            )

        _run_alembic(database_url, QUOTA_REVISION)

        with engine.connect() as connection:
            providers = dict(
                connection.exec_driver_sql("SELECT id, runtime_provider FROM batch_generation_tasks ORDER BY id").all()
            )
            assert providers == {
                "task-default": "gemini",
                "task-override": "anthropic",
                "task-terminal": None,
            }
            inspector = sa.inspect(connection)
            assert "batch_generation_quota_guards" in inspector.get_table_names()
            index_names = {str(index["name"]) for index in inspector.get_indexes("batch_generation_tasks")}
            assert {
                "ix_batch_generation_tasks_project_status",
                "ix_batch_generation_tasks_actor_status",
                "ix_batch_generation_tasks_provider_status",
            }.issubset(index_names)
            assert (
                str(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one())
                == QUOTA_REVISION
            )

        config = migrations._alembic_config(database_url=database_url)
        _run_alembic(database_url, "head")
        command.check(config)

        _run_alembic(database_url, PREVIOUS_REVISION, downgrade=True)
        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            assert "batch_generation_quota_guards" not in inspector.get_table_names()
            assert "runtime_provider" not in {
                str(column["name"]) for column in inspector.get_columns("batch_generation_tasks")
            }
            assert (
                str(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one())
                == PREVIOUS_REVISION
            )
    finally:
        engine.dispose()


def test_quota_migration_fails_before_ddl_when_active_provider_cannot_be_resolved(tmp_path: Path) -> None:
    database_path = tmp_path / "quota-migration-unresolved.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    _run_alembic(database_url, PREVIOUS_REVISION)
    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            _seed_project(
                connection,
                project_id="p-unconfigured",
                user_id="u-unconfigured",
                status="queued",
                task_id="task-unconfigured",
            )

        with pytest.raises(RuntimeError, match=r"active tasks lack.*task-unconfigured=<missing>"):
            _run_alembic(database_url, QUOTA_REVISION)

        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            assert "batch_generation_quota_guards" not in inspector.get_table_names()
            assert "runtime_provider" not in {
                str(column["name"]) for column in inspector.get_columns("batch_generation_tasks")
            }
            assert (
                str(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one())
                == PREVIOUS_REVISION
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("status", "provider", "message"),
    [
        ("queued", "OpenAI", r"valid chapter_generate.*task-invalid=OpenAI"),
        ("running", "openai", r"tasks are running.*task-invalid"),
    ],
)
def test_quota_migration_fails_closed_for_noncanonical_or_running_active_tasks(
    tmp_path: Path,
    status: str,
    provider: str,
    message: str,
) -> None:
    database_path = tmp_path / f"quota-migration-{status}-{provider}.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    _run_alembic(database_url, PREVIOUS_REVISION)
    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            _seed_project(
                connection,
                project_id="p-invalid",
                user_id="u-invalid",
                status=status,
                task_id="task-invalid",
            )
            connection.exec_driver_sql(
                "INSERT INTO llm_presets (project_id, provider, model) VALUES ('p-invalid', ?, 'gpt-4o-mini')",
                (provider,),
            )

        with pytest.raises(RuntimeError, match=message):
            _run_alembic(database_url, QUOTA_REVISION)

        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            assert "batch_generation_quota_guards" not in inspector.get_table_names()
            assert "runtime_provider" not in {
                str(column["name"]) for column in inspector.get_columns("batch_generation_tasks")
            }
    finally:
        engine.dispose()
