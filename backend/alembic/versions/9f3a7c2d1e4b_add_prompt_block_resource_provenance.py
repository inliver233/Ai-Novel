"""add prompt block resource provenance

Revision ID: 9f3a7c2d1e4b
Revises: 5da9e95bd9a3
Create Date: 2026-07-10 00:00:00.000000

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "9f3a7c2d1e4b"
down_revision = "5da9e95bd9a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("prompt_blocks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("origin_template_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("resource_template_outdated", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("prompt_blocks", schema=None) as batch_op:
        batch_op.drop_column("resource_template_outdated")
        batch_op.drop_column("origin_template_hash")
