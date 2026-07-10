from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from alembic import command
from sqlalchemy import Boolean, String, create_engine, inspect

from app.db import migrations


PREVIOUS_REVISION = "5da9e95bd9a3"
PROVENANCE_REVISION = "9f3a7c2d1e4b"


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


def test_prompt_block_resource_provenance_migration_round_trip() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        database_path = Path(temp_dir) / "prompt-provenance.db"
        database_url = f"sqlite:///{database_path.as_posix()}"

        _run_alembic(database_url, PREVIOUS_REVISION)

        connection = sqlite3.connect(str(database_path))
        try:
            connection.execute(
                """
                INSERT INTO prompt_blocks (
                    id, preset_id, identifier, name, role, enabled, template, marker_key,
                    injection_position, injection_depth, injection_order, triggers_json,
                    forbid_overrides, budget_json, cache_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-block",
                    "legacy-preset",
                    "legacy.identifier",
                    "Legacy block",
                    "system",
                    1,
                    "LEGACY_TEMPLATE_MUST_SURVIVE",
                    None,
                    "relative",
                    None,
                    0,
                    "[]",
                    0,
                    None,
                    None,
                    "2026-07-10T00:00:00+00:00",
                    "2026-07-10T00:00:00+00:00",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        _run_alembic(database_url, PROVENANCE_REVISION)

        engine = create_engine(database_url)
        try:
            columns = {column["name"]: column for column in inspect(engine).get_columns("prompt_blocks")}
            assert columns["origin_template_hash"]["nullable"] is True
            assert columns["resource_template_outdated"]["nullable"] is True
            assert isinstance(columns["origin_template_hash"]["type"], String)
            assert columns["origin_template_hash"]["type"].length == 64
            assert isinstance(columns["resource_template_outdated"]["type"], Boolean)

            with engine.connect() as connection:
                row = connection.exec_driver_sql(
                    """
                    SELECT template, origin_template_hash, resource_template_outdated
                    FROM prompt_blocks
                    WHERE id = 'legacy-block'
                    """
                ).one()
            assert row == ("LEGACY_TEMPLATE_MUST_SURVIVE", None, None)
        finally:
            engine.dispose()

        _run_alembic(database_url, PREVIOUS_REVISION, downgrade=True)

        engine = create_engine(database_url)
        try:
            column_names = {column["name"] for column in inspect(engine).get_columns("prompt_blocks")}
            assert "origin_template_hash" not in column_names
            assert "resource_template_outdated" not in column_names
            with engine.connect() as connection:
                template = connection.exec_driver_sql(
                    "SELECT template FROM prompt_blocks WHERE id = 'legacy-block'"
                ).scalar_one()
            assert template == "LEGACY_TEMPLATE_MUST_SURVIVE"
        finally:
            engine.dispose()
