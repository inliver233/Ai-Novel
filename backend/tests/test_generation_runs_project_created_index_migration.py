"""backend-data#8：generation_runs 需要 (project_id, created_at) 复合索引。

列表查询按 project_id 过滤 + created_at desc 排序（api/routes/generation_runs.py），
单列索引无法覆盖排序。迁移必须可逆且不影响既有索引。
"""

from __future__ import annotations

import os
from pathlib import Path

import sqlalchemy as sa
from alembic import command

from app.db import migrations

PREVIOUS_REVISION = "e8b3c5d7f9a1"
INDEX_REVISION = "b4d8f2a6c1e3"
_INDEX_NAME = "ix_generation_runs_project_id_created_at"


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


def _index_map(engine: sa.Engine) -> dict[str, list[str]]:
    inspector = sa.inspect(engine)
    return {index["name"]: list(index["column_names"]) for index in inspector.get_indexes("generation_runs")}


def test_index_migration_creates_composite_and_downgrade_preserves_existing(tmp_path: Path) -> None:
    database_path = tmp_path / "generation-runs-index.db"
    database_url = f"sqlite:///{database_path.as_posix()}"

    _run_alembic(database_url, PREVIOUS_REVISION)
    engine = sa.create_engine(database_url)
    try:
        before = _index_map(engine)
        assert _INDEX_NAME not in before
        assert "ix_generation_runs_project_id" in before

        _run_alembic(database_url, INDEX_REVISION)
        after = _index_map(engine)
        assert after[_INDEX_NAME] == ["project_id", "created_at"]

        _run_alembic(database_url, PREVIOUS_REVISION, downgrade=True)
        downgraded = _index_map(engine)
        assert _INDEX_NAME not in downgraded
        assert "ix_generation_runs_project_id" in downgraded
        assert "ix_generation_runs_actor_user_id_created_at" in downgraded
    finally:
        engine.dispose()


def test_model_declares_composite_index() -> None:
    from app.models.generation_run import GenerationRun

    names = {index.name for index in GenerationRun.__table__.indexes}
    assert _INDEX_NAME in names
