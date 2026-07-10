"""reconcile active schema metadata

Revision ID: c7e2a4f6b8d0
Revises: b2d4e6f8a0c1
Create Date: 2026-07-11 00:00:00.000000

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c7e2a4f6b8d0"
down_revision = "b2d4e6f8a0c1"
branch_labels = None
depends_on = None


_USER_ID_COLUMNS = (
    ("batch_generation_tasks", "actor_user_id", True),
    ("vector_rag_profiles", "owner_user_id", False),
    ("writing_styles", "owner_user_id", True),
)

_BOOLEAN_SERVER_DEFAULTS = (
    ("users", "is_admin", sa.false()),
    ("project_settings", "context_optimizer_enabled", sa.false()),
    ("project_settings", "auto_update_worldbook_enabled", sa.true()),
    ("project_settings", "auto_update_characters_enabled", sa.true()),
    ("project_settings", "auto_update_story_memory_enabled", sa.true()),
    ("project_settings", "auto_update_graph_enabled", sa.true()),
    ("project_settings", "auto_update_vector_enabled", sa.true()),
    ("project_settings", "auto_update_search_enabled", sa.true()),
    ("project_settings", "auto_update_fractal_enabled", sa.true()),
    ("project_settings", "auto_update_tables_enabled", sa.true()),
    ("project_settings", "vector_index_dirty", sa.false()),
)


def _require_user_ids_fit_downgraded_width() -> None:
    bind = op.get_bind()
    quote = bind.dialect.identifier_preparer.quote
    oversized: list[tuple[str, str, int]] = []
    for table_name, column_name, _nullable in _USER_ID_COLUMNS:
        max_length = bind.exec_driver_sql(
            f"SELECT MAX(LENGTH({quote(column_name)})) FROM {quote(table_name)}"
        ).scalar_one_or_none()
        if max_length is not None and int(max_length) > 36:
            oversized.append((table_name, column_name, int(max_length)))

    if oversized:
        details = ", ".join(f"{table}.{column}=length {length}" for table, column, length in oversized)
        raise RuntimeError("cannot downgrade reconciled user-id columns to VARCHAR(36) without data loss: " + details)


def _set_persistent_boolean_defaults(*, enabled: bool) -> None:
    # The historical add-column migrations removed these defaults on
    # PostgreSQL after backfill but could not cheaply do so on SQLite.  They are
    # application invariants, not temporary migration values, so the new head
    # makes them persistent on both dialects.  SQLite already has the exact
    # defaults from those historical migrations and needs no table rebuild.
    if op.get_bind().dialect.name == "sqlite":
        return

    for table_name, column_name, default in _BOOLEAN_SERVER_DEFAULTS:
        op.alter_column(
            table_name,
            column_name,
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=default if enabled else None,
        )


def _alter_user_id_width(*, length: int) -> None:
    previous_length = 36 if length == 64 else 64
    for table_name, column_name, nullable in _USER_ID_COLUMNS:
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            batch_op.alter_column(
                column_name,
                existing_type=sa.String(length=previous_length),
                type_=sa.String(length=length),
                existing_nullable=nullable,
            )


def upgrade() -> None:
    _alter_user_id_width(length=64)
    _set_persistent_boolean_defaults(enabled=True)
    # The three pre-existing indexes are intentionally not recreated here:
    # their original migrations already created them.  The ORM metadata now
    # declares them, which removes the false "remove_index" autogenerate diff.


def downgrade() -> None:
    _require_user_ids_fit_downgraded_width()
    _set_persistent_boolean_defaults(enabled=False)
    _alter_user_id_width(length=36)
