from __future__ import annotations

import os
from pathlib import Path

import sqlalchemy as sa
from alembic import command

from app.db import migrations


def _upgrade(database_url: str, revision: str) -> None:
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(migrations._alembic_config(database_url=database_url), revision)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def test_user_disabled_at_backfills_from_legacy_password_row(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'disabled.db').as_posix()}"
    _upgrade(database_url, "a4c9d2e7f1b3")
    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO users (id, display_name, is_admin, created_at, updated_at) "
                "VALUES ('disabled-user', 'Disabled', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            connection.exec_driver_sql(
                "INSERT INTO user_passwords "
                "(user_id, password_hash, password_updated_at, disabled_at, created_at, updated_at) "
                "VALUES ('disabled-user', 'hash', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        _upgrade(database_url, "head")
        with engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT disabled_at, session_invalid_before, session_version FROM users WHERE id = 'disabled-user'"
            ).one()
            assert row[0] is not None
            assert row[1] is not None
            assert row[2] == 0
    finally:
        engine.dispose()
