"""add batch generation heartbeat lease column

Revision ID: e8b3c5d7f9a1
Revises: d1f6a9b3c8e2
Create Date: 2026-07-26

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "e8b3c5d7f9a1"
down_revision = "d1f6a9b3c8e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("batch_generation_tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("batch_generation_tasks", schema=None) as batch_op:
        batch_op.drop_column("heartbeat_at")
