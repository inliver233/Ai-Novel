"""add pgvector vector_chunks table

Revision ID: 4858b08a6519
Revises: f89622011fdb
Create Date: 2026-01-10

"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "4858b08a6519"
down_revision = "f89622011fdb"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    bind = op.get_bind()
    return getattr(getattr(bind, "dialect", None), "name", "") == "postgresql"

def _pgvector_extension_available() -> bool:
    bind = op.get_bind()
    return bool(bind.execute(text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")).scalar())


def upgrade() -> None:
    if not _is_postgres():
        return

    # This migration is Postgres-only; SQLite environments keep using Chroma.
    # On PostgreSQL the pgvector extension is a hard requirement: a fresh database
    # must not silently complete the chain without vector_chunks (backend-data#4).
    if not _pgvector_extension_available():
        raise RuntimeError(
            "pgvector extension is unavailable; install pgvector on the PostgreSQL server before upgrading"
        )

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS vector_chunks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            title TEXT,
            chapter_number INTEGER,
            text_md TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            embedding vector(1536) NOT NULL,
            content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text_md, ''))) STORED,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """.strip()
    )

    op.execute("CREATE INDEX IF NOT EXISTS ix_vector_chunks_project_id ON vector_chunks(project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_vector_chunks_project_id_source ON vector_chunks(project_id, source)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_vector_chunks_content_tsv ON vector_chunks USING GIN(content_tsv)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_vector_chunks_embedding_ivfflat "
        "ON vector_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )


def downgrade() -> None:
    if not _is_postgres():
        return

    op.execute("DROP TABLE IF EXISTS vector_chunks")
    op.execute("DROP EXTENSION IF EXISTS vector")
