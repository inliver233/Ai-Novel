"""add batch generation quota guards

Revision ID: f3a1c7e9b2d4
Revises: d8f3b5a7c9e1
Create Date: 2026-07-11 00:00:00.000000

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "f3a1c7e9b2d4"
down_revision = "d8f3b5a7c9e1"
branch_labels = None
depends_on = None


_SNAPSHOTTABLE_ACTIVE_STATUSES = ("queued", "paused")
_CANONICAL_PROVIDERS = {
    "openai",
    "openai_responses",
    "openai_compatible",
    "openai_responses_compatible",
    "anthropic",
    "gemini",
}
_PROVIDER_ALIASES = {
    "openai-chat": "openai",
    "openai-responses": "openai_responses",
    "openai_compat": "openai_compatible",
    "openai-compatible": "openai_compatible",
    "openai_responses_compat": "openai_responses_compatible",
    "openai-responses-compatible": "openai_responses_compatible",
    "google": "gemini",
}
_RESOLVED_PROVIDER_SQL = """
CASE
    WHEN EXISTS (
        SELECT 1
        FROM llm_task_presets AS task_preset
        WHERE task_preset.project_id = batch_generation_tasks.project_id
          AND task_preset.task_key = 'chapter_generate'
    ) THEN (
        SELECT NULLIF(TRIM(task_preset.provider), '')
        FROM llm_task_presets AS task_preset
        WHERE task_preset.project_id = batch_generation_tasks.project_id
          AND task_preset.task_key = 'chapter_generate'
    )
    ELSE (
        SELECT NULLIF(TRIM(project_preset.provider), '')
        FROM llm_presets AS project_preset
        WHERE project_preset.project_id = batch_generation_tasks.project_id
    )
END
"""
_NORMALIZED_PROVIDER_KEYS = {provider: provider for provider in _CANONICAL_PROVIDERS} | _PROVIDER_ALIASES
_CANONICAL_PROVIDER_SQL = (
    "CASE "
    + " ".join(
        f"WHEN ({_RESOLVED_PROVIDER_SQL}) = '{source}' THEN '{canonical}'"
        for source, canonical in sorted(_NORMALIZED_PROVIDER_KEYS.items())
    )
    + " ELSE NULL END"
)


def _require_resolvable_provider_for_active_tasks() -> None:
    bind = op.get_bind()
    running = bind.execute(
        sa.text(
            """
            SELECT id
            FROM batch_generation_tasks
            WHERE status = 'running'
            ORDER BY id
            """
        )
    ).scalars().all()
    if running:
        raise RuntimeError(
            "cannot snapshot batch runtime providers while tasks are running; drain them before upgrade: "
            + ", ".join(str(task_id) for task_id in running)
        )

    active_rows = bind.execute(
        sa.text(
            f"""
            SELECT
                id,
                ({_RESOLVED_PROVIDER_SQL}) AS configured_provider,
                ({_CANONICAL_PROVIDER_SQL}) AS runtime_provider
            FROM batch_generation_tasks
            WHERE status IN :active_statuses
            ORDER BY id
            """
        ).bindparams(sa.bindparam("active_statuses", expanding=True)),
            {"active_statuses": _SNAPSHOTTABLE_ACTIVE_STATUSES},
    ).all()
    invalid = [
        (str(task_id), str(configured_provider or ""))
        for task_id, configured_provider, runtime_provider in active_rows
        if str(runtime_provider or "") not in _CANONICAL_PROVIDERS
    ]
    if invalid:
        details = ", ".join(f"{task_id}={provider or '<missing>'}" for task_id, provider in invalid)
        raise RuntimeError(
            "cannot add authoritative batch runtime providers while active tasks lack a "
            "valid chapter_generate task preset or project LLM preset: " + details
        )


def upgrade() -> None:
    _require_resolvable_provider_for_active_tasks()

    with op.batch_alter_table("batch_generation_tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("runtime_provider", sa.String(length=64), nullable=True))

    op.execute(
        sa.text(
            f"""
            UPDATE batch_generation_tasks
            SET runtime_provider = ({_CANONICAL_PROVIDER_SQL})
            WHERE runtime_provider IS NULL
            """
        )
    )

    op.create_index(
        "ix_batch_generation_tasks_project_status",
        "batch_generation_tasks",
        ["project_id", "status"],
    )
    op.create_index(
        "ix_batch_generation_tasks_actor_status",
        "batch_generation_tasks",
        ["actor_user_id", "status"],
    )
    op.create_index(
        "ix_batch_generation_tasks_provider_status",
        "batch_generation_tasks",
        ["runtime_provider", "status"],
    )
    op.create_table(
        "batch_generation_quota_guards",
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_key", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("scope_type", "scope_key"),
    )


def downgrade() -> None:
    op.drop_table("batch_generation_quota_guards")
    op.drop_index("ix_batch_generation_tasks_provider_status", table_name="batch_generation_tasks")
    op.drop_index("ix_batch_generation_tasks_actor_status", table_name="batch_generation_tasks")
    op.drop_index("ix_batch_generation_tasks_project_status", table_name="batch_generation_tasks")
    with op.batch_alter_table("batch_generation_tasks", schema=None) as batch_op:
        batch_op.drop_column("runtime_provider")
