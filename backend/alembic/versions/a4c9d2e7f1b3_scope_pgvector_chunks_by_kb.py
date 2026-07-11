"""scope pgvector chunks by knowledge base

Revision ID: a4c9d2e7f1b3
Revises: f3a1c7e9b2d4
Create Date: 2026-07-11 00:00:00.000000

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a4c9d2e7f1b3"
down_revision = "f3a1c7e9b2d4"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    bind = op.get_bind()
    return getattr(getattr(bind, "dialect", None), "name", "") == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute("ALTER TABLE vector_chunks ADD COLUMN kb_id VARCHAR(64) NOT NULL DEFAULT 'default'")
    op.execute("ALTER TABLE vector_chunks DROP CONSTRAINT vector_chunks_pkey")
    op.execute("ALTER TABLE vector_chunks ADD CONSTRAINT vector_chunks_pkey PRIMARY KEY (project_id, kb_id, id)")
    op.execute("DROP INDEX IF EXISTS ix_vector_chunks_project_id")
    op.execute("DROP INDEX IF EXISTS ix_vector_chunks_project_id_source")
    op.execute("CREATE INDEX ix_vector_chunks_project_kb ON vector_chunks(project_id, kb_id)")
    op.execute("CREATE INDEX ix_vector_chunks_project_kb_source ON vector_chunks(project_id, kb_id, source)")


def downgrade() -> None:
    if not _is_postgres():
        return

    bind = op.get_bind()
    non_default = int(bind.execute(sa.text("SELECT COUNT(*) FROM vector_chunks WHERE kb_id <> 'default'")).scalar_one())
    if non_default:
        raise RuntimeError("cannot downgrade scoped pgvector chunks while non-default knowledge-base data exists")
    duplicate_ids = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM ("
                "SELECT id FROM vector_chunks GROUP BY id HAVING COUNT(*) > 1"
                ") AS duplicate_vector_chunk_ids"
            )
        ).scalar_one()
    )
    if duplicate_ids:
        raise RuntimeError("cannot downgrade scoped pgvector chunks while chunk ids are duplicated across projects")

    op.execute("DROP INDEX IF EXISTS ix_vector_chunks_project_kb_source")
    op.execute("DROP INDEX IF EXISTS ix_vector_chunks_project_kb")
    op.execute("ALTER TABLE vector_chunks DROP CONSTRAINT vector_chunks_pkey")
    op.execute("ALTER TABLE vector_chunks ADD CONSTRAINT vector_chunks_pkey PRIMARY KEY (id)")
    op.execute("ALTER TABLE vector_chunks DROP COLUMN kb_id")
    op.execute("CREATE INDEX ix_vector_chunks_project_id ON vector_chunks(project_id)")
    op.execute("CREATE INDEX ix_vector_chunks_project_id_source ON vector_chunks(project_id, source)")
