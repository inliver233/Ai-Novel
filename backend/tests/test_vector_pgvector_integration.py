from __future__ import annotations

import unittest
import uuid

from sqlalchemy import text

from app.db.session import engine
from app.services import vector_rag_service


class TestVectorPgvectorIntegration(unittest.TestCase):
    """
    Optional pgvector integration test (Postgres-only).

    How to run (PowerShell example):
      1) Start a Postgres with pgvector (example):
         docker run --rm -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=ainovel_test -p 5432:5432 --name ainovel-pgvector-test pgvector/pgvector:pg16
      2) $env:DATABASE_URL = "postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/ainovel_test"
      3) cd backend; .\\.venv\\Scripts\\python.exe -m unittest -v tests.test_vector_pgvector_integration
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not vector_rag_service._is_postgres():
            raise unittest.SkipTest("not_postgres (set DATABASE_URL to a postgresql+psycopg2 URL to run)")

        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            raise unittest.SkipTest(f"postgres_unreachable:{type(exc).__name__}")

        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS users (
                            id TEXT PRIMARY KEY,
                            email TEXT UNIQUE,
                            password_hash TEXT,
                            display_name TEXT,
                            is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """.strip()
                    )
                )
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS projects (
                            id TEXT PRIMARY KEY,
                            owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                            name TEXT NOT NULL,
                            genre TEXT,
                            logline TEXT,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """.strip()
                    )
                )
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS vector_chunks (
                            id TEXT NOT NULL,
                            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                            kb_id VARCHAR(64) NOT NULL DEFAULT 'default',
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
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            PRIMARY KEY (project_id, kb_id, id)
                        )
                        """.strip()
                    )
                )
        except Exception as exc:
            raise unittest.SkipTest(f"pgvector_unavailable:{type(exc).__name__}")

        cls.project_id = str(uuid.uuid4())
        cls.user_id = f"pgv_user_{uuid.uuid4()}"

        cls._ensure_clean_project()
        cls._ensure_test_project()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._ensure_clean_project()
        except Exception:
            pass

    @classmethod
    def _ensure_clean_project(cls) -> None:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM vector_chunks WHERE project_id = :pid"), {"pid": cls.project_id})
            conn.execute(text("DELETE FROM projects WHERE id = :pid"), {"pid": cls.project_id})
            conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": cls.user_id})

    @classmethod
    def _ensure_test_project(cls) -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO users (id, display_name, created_at, updated_at)
                    VALUES (:uid, 'pgvector-test', NOW(), NOW())
                    ON CONFLICT (id) DO NOTHING
                    """.strip()
                ),
                {"uid": cls.user_id},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO projects (id, owner_user_id, name, created_at, updated_at)
                    VALUES (:pid, :uid, 'pgvector-test', NOW(), NOW())
                    ON CONFLICT (id) DO NOTHING
                    """.strip()
                ),
                {"pid": cls.project_id, "uid": cls.user_id},
            )

    def test_pgvector_hybrid_fetch_binds_sources_array(self) -> None:
        def emb(first: float) -> list[float]:
            return [first, *([0.0] * 1535)]

        chunks = [
            vector_rag_service.VectorChunk(
                id="c_memory",
                text="dragon from story memory",
                metadata={"source": "story_memory", "source_id": "m1", "chunk_index": 0, "title": "M"},
            ),
            vector_rag_service.VectorChunk(
                id="c_outline",
                text="dragon from outline",
                metadata={"source": "outline", "source_id": "ol1", "chunk_index": 0, "title": "OL"},
            ),
            vector_rag_service.VectorChunk(
                id="c_chapter",
                text="dragon from chapter",
                metadata={"source": "chapter", "source_id": "ch1", "chunk_index": 0, "title": "CH"},
            ),
        ]
        embeddings = [emb(0.0), emb(100.0), emb(10.0)]
        out = vector_rag_service._pgvector_upsert_chunks(
            project_id=self.project_id, kb_id="default", chunks=chunks, embeddings=embeddings
        )
        self.assertTrue(out.get("enabled"))
        self.assertFalse(out.get("skipped"))
        self.assertEqual(int(out.get("ingested") or 0), 3)

        fetch = vector_rag_service._pgvector_hybrid_fetch(
            project_id=self.project_id,
            kb_id="default",
            query_text="dragon",
            query_vec=emb(0.0),
            sources=["story_memory", "chapter"],
            vector_k=10,
            fts_k=10,
            rrf_k=60,
        )
        self.assertIsInstance(fetch, dict)
        candidates = fetch.get("candidates")
        self.assertIsInstance(candidates, list)
        cand_ids = {str(c.get("id")) for c in candidates if isinstance(c, dict)}

        self.assertIn("c_memory", cand_ids)
        self.assertIn("c_chapter", cand_ids)
        self.assertNotIn("c_outline", cand_ids)

        counts = fetch.get("counts") if isinstance(fetch.get("counts"), dict) else {}
        self.assertEqual(int(counts.get("union") or 0), 2)
