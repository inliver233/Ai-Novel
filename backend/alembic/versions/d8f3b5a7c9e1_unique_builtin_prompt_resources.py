"""enforce unique builtin prompt resources

Revision ID: d8f3b5a7c9e1
Revises: c7e2a4f6b8d0
Create Date: 2026-07-11 00:00:00.000000

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d8f3b5a7c9e1"
down_revision = "c7e2a4f6b8d0"
branch_labels = None
depends_on = None


def _require_no_duplicates() -> None:
    bind = op.get_bind()
    duplicate_presets = bind.execute(
        sa.text(
            """
            SELECT project_id, resource_key, COUNT(*) AS duplicate_count
            FROM prompt_presets
            WHERE resource_key IS NOT NULL
            GROUP BY project_id, resource_key
            HAVING COUNT(*) > 1
            ORDER BY project_id, resource_key
            """
        )
    ).all()
    if duplicate_presets:
        preset_details = ", ".join(
            f"project_id={project_id!r} resource_key={resource_key!r} count={count}"
            for project_id, resource_key, count in duplicate_presets
        )
        raise RuntimeError(
            "cannot add builtin prompt uniqueness constraints while duplicate rows exist; "
            "resolve the duplicates without discarding user content and rerun the migration: "
            + f"prompt_presets[{preset_details}]"
        )


def upgrade() -> None:
    _require_no_duplicates()
    with op.batch_alter_table("prompt_presets", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_prompt_presets_project_resource_key",
            ["project_id", "resource_key"],
        )


def downgrade() -> None:
    with op.batch_alter_table("prompt_presets", schema=None) as batch_op:
        batch_op.drop_constraint("uq_prompt_presets_project_resource_key", type_="unique")
