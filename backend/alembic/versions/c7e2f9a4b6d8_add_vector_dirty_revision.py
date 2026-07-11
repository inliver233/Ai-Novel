"""add project settings vector dirty revision

Revision ID: c7e2f9a4b6d8
Revises: b6d1e8f3a5c7
"""

from alembic import op
import sqlalchemy as sa

revision = "c7e2f9a4b6d8"
down_revision = "b6d1e8f3a5c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_settings",
        sa.Column("vector_dirty_revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("project_settings", "vector_dirty_revision")
