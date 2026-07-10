from __future__ import annotations

import os
import re
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine.url import make_url

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.append(str(BASE_DIR))

load_dotenv(BASE_DIR / ".env")

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

from app.db.base import Base  # noqa: E402
from app.db.alembic_compare import include_object_for_dialect  # noqa: E402
import app.models  # noqa: F401,E402

target_metadata = Base.metadata


def _is_abs_path(value: str) -> bool:
    if value.startswith("/"):
        return True
    if value.startswith("\\\\"):
        return True
    return bool(re.match(r"^[A-Za-z]:[\\/]", value))


def _normalize_database_url(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return value

    try:
        url = make_url(value)
    except Exception:
        return value

    if url.get_backend_name() != "sqlite":
        return value

    db = str(url.database or "").strip()
    if not db or db == ":memory:" or db.startswith("file:"):
        return value
    if _is_abs_path(db):
        return value

    abs_path = (BASE_DIR / db).resolve()
    return str(url.set(database=abs_path.as_posix()))


def _get_database_url() -> str:
    raw = os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    return _normalize_database_url(raw)


def run_migrations_offline() -> None:
    url = _get_database_url()
    dialect_name = make_url(url).get_backend_name()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object_for_dialect(dialect_name),
        render_as_batch=dialect_name == "sqlite",
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _get_database_url()
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = url

    existing_connection = config.attributes.get("connection")
    if existing_connection is not None:
        dialect_name = existing_connection.dialect.name
        context.configure(
            connection=existing_connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=include_object_for_dialect(dialect_name),
            render_as_batch=dialect_name == "sqlite",
        )

        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        dialect_name = connection.dialect.name
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=include_object_for_dialect(dialect_name),
            render_as_batch=dialect_name == "sqlite",
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
