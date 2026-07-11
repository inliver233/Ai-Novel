"""add embedding dimension contract and trigram lexical index

Revision ID: d1f6a9b3c8e2
Revises: c7e2f9a4b6d8
"""

from alembic import op
import sqlalchemy as sa


revision = "d1f6a9b3c8e2"
down_revision = "c7e2f9a4b6d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_settings",
        sa.Column("vector_embedding_expected_dimension", sa.Integer(), server_default="1536", nullable=False),
    )
    op.add_column(
        "vector_rag_profiles",
        sa.Column("vector_embedding_expected_dimension", sa.Integer(), server_default="1536", nullable=False),
    )
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_vector_chunks_text_md_trgm ON vector_chunks USING GIN (text_md gin_trgm_ops)"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_vector_chunks_text_md_trgm")
    op.drop_column("vector_rag_profiles", "vector_embedding_expected_dimension")
    op.drop_column("project_settings", "vector_embedding_expected_dimension")
