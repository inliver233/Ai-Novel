"""add generation_runs project/created composite index

Revision ID: b4d8f2a6c1e3
Revises: e8b3c5d7f9a1
Create Date: 2026-07-28

"""

from __future__ import annotations

from alembic import op


revision = "b4d8f2a6c1e3"
down_revision = "e8b3c5d7f9a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_generation_runs_project_id_created_at",
        "generation_runs",
        ["project_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_generation_runs_project_id_created_at", table_name="generation_runs")
