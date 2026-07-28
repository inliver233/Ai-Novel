"""add project task cancel_requested flag

Revision ID: c6e2a8b4d9f1
Revises: b4d8f2a6c1e3
Create Date: 2026-07-28

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c6e2a8b4d9f1"
down_revision = "b4d8f2a6c1e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("project_tasks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("project_tasks", schema=None) as batch_op:
        batch_op.drop_column("cancel_requested")
