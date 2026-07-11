"""add authoritative users.disabled_at

Revision ID: b6d1e8f3a5c7
Revises: a4c9d2e7f1b3
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "b6d1e8f3a5c7"
down_revision = "a4c9d2e7f1b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("session_invalid_before", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("session_version", sa.Integer(), nullable=False, server_default="0"))
    op.execute(
        sa.text(
            """
            UPDATE users
            SET disabled_at = (SELECT user_passwords.disabled_at FROM user_passwords WHERE user_passwords.user_id = users.id),
                session_invalid_before = (SELECT user_passwords.disabled_at FROM user_passwords WHERE user_passwords.user_id = users.id)
            WHERE EXISTS (
                SELECT 1 FROM user_passwords
                WHERE user_passwords.user_id = users.id
                  AND user_passwords.disabled_at IS NOT NULL
            )
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("session_version")
        batch_op.drop_column("session_invalid_before")
        batch_op.drop_column("disabled_at")
