from __future__ import annotations

import unittest

from app.core.config import settings
from app.services import vector_rag_service


class TestVectorHybridRrf(unittest.TestCase):
    def test_rrf_score_prefers_better_rank(self) -> None:
        a = vector_rag_service._rrf_score(vector_rank=1, fts_rank=None, k=60)
        b = vector_rag_service._rrf_score(vector_rank=10, fts_rank=None, k=60)
        self.assertGreater(a, b)

    def test_rrf_score_combines_two_lists(self) -> None:
        only_vec = vector_rag_service._rrf_score(vector_rank=1, fts_rank=None, k=60)
        only_fts = vector_rag_service._rrf_score(vector_rank=None, fts_rank=1, k=60)
        both = vector_rag_service._rrf_score(vector_rank=1, fts_rank=1, k=60)
        self.assertGreater(both, only_vec)
        self.assertGreater(both, only_fts)

    def test_pgvector_literal_format(self) -> None:
        lit = vector_rag_service._pgvector_literal([1.0, 2.5, -3.125])
        self.assertTrue(lit.startswith("["))
        self.assertTrue(lit.endswith("]"))
        self.assertIn(",", lit)

    def test_overfiltering_relax_sources_then_expand_candidates(self) -> None:
        orig_is_postgres = vector_rag_service._is_postgres
        orig_fetch = vector_rag_service._pgvector_hybrid_fetch
        orig_overfilter = getattr(settings, "vector_overfiltering_enabled", True)
        orig_max_candidates = getattr(settings, "vector_max_candidates", None)
        orig_final_max_chunks = getattr(settings, "vector_final_max_chunks", None)

        calls: list[dict[str, object]] = []

        def _fake_fetch(
            *,
            project_id: str,
            query_text: str,
            query_vec: list[float],
            sources: list[vector_rag_service.VectorSource],
            vector_k: int,
            fts_k: int,
            rrf_k: int,
        ) -> dict[str, object]:
            calls.append({"sources": list(sources), "vector_k": int(vector_k), "fts_k": int(fts_k), "rrf_k": int(rrf_k)})
            return {"candidates": [], "ranks": {}, "counts": {"union": 0}}

        try:
            vector_rag_service._is_postgres = lambda: True  # type: ignore[assignment]
            vector_rag_service._pgvector_hybrid_fetch = _fake_fetch  # type: ignore[assignment]
            settings.vector_overfiltering_enabled = True
            settings.vector_max_candidates = 20
            settings.vector_final_max_chunks = 6

            out = vector_rag_service._pgvector_hybrid_query(
                project_id="p1", query_text="hello", query_vec=[0.1], sources=["outline"]
            )
            overfilter = out.get("overfilter")
            self.assertIsInstance(overfilter, dict)
            self.assertEqual(overfilter.get("actions"), ["relax_sources", "expand_candidates"])

            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[0]["sources"], ["outline"])
            # lite 的规范全集只有三个活源；worldbook 是已删除功能的幽灵源，不能
            # 被 overfilter relax 重新引入。
            self.assertEqual(calls[1]["sources"], ["outline", "chapter", "story_memory"])
            self.assertEqual(calls[2]["vector_k"], 60)
            self.assertEqual(calls[2]["fts_k"], 60)
        finally:
            vector_rag_service._is_postgres = orig_is_postgres  # type: ignore[assignment]
            vector_rag_service._pgvector_hybrid_fetch = orig_fetch  # type: ignore[assignment]
            settings.vector_overfiltering_enabled = orig_overfilter
            settings.vector_max_candidates = orig_max_candidates
            settings.vector_final_max_chunks = orig_final_max_chunks

    def test_overfiltering_stops_when_enough_union(self) -> None:
        orig_is_postgres = vector_rag_service._is_postgres
        orig_fetch = vector_rag_service._pgvector_hybrid_fetch
        orig_overfilter = getattr(settings, "vector_overfiltering_enabled", True)
        orig_max_candidates = getattr(settings, "vector_max_candidates", None)
        orig_final_max_chunks = getattr(settings, "vector_final_max_chunks", None)

        calls: list[dict[str, object]] = []

        def _fake_fetch(
            *,
            project_id: str,
            query_text: str,
            query_vec: list[float],
            sources: list[vector_rag_service.VectorSource],
            vector_k: int,
            fts_k: int,
            rrf_k: int,
        ) -> dict[str, object]:
            calls.append({"sources": list(sources), "vector_k": int(vector_k), "fts_k": int(fts_k)})
            return {"candidates": [], "ranks": {}, "counts": {"union": 3}}

        try:
            vector_rag_service._is_postgres = lambda: True  # type: ignore[assignment]
            vector_rag_service._pgvector_hybrid_fetch = _fake_fetch  # type: ignore[assignment]
            settings.vector_overfiltering_enabled = True
            settings.vector_max_candidates = 20
            settings.vector_final_max_chunks = 6

            out = vector_rag_service._pgvector_hybrid_query(
                project_id="p1", query_text="hello", query_vec=[0.1], sources=["outline"]
            )
            overfilter = out.get("overfilter")
            self.assertIsInstance(overfilter, dict)
            self.assertEqual(overfilter.get("actions"), [])
            self.assertEqual(overfilter.get("used_sources"), ["outline"])
            self.assertEqual(len(calls), 1)
        finally:
            vector_rag_service._is_postgres = orig_is_postgres  # type: ignore[assignment]
            vector_rag_service._pgvector_hybrid_fetch = orig_fetch  # type: ignore[assignment]
            settings.vector_overfiltering_enabled = orig_overfilter
            settings.vector_max_candidates = orig_max_candidates
            settings.vector_final_max_chunks = orig_final_max_chunks

    def test_overfiltering_disabled_breaks_immediately(self) -> None:
        orig_is_postgres = vector_rag_service._is_postgres
        orig_fetch = vector_rag_service._pgvector_hybrid_fetch
        orig_overfilter = getattr(settings, "vector_overfiltering_enabled", True)

        calls: list[dict[str, object]] = []

        def _fake_fetch(
            *,
            project_id: str,
            query_text: str,
            query_vec: list[float],
            sources: list[vector_rag_service.VectorSource],
            vector_k: int,
            fts_k: int,
            rrf_k: int,
        ) -> dict[str, object]:
            calls.append({"sources": list(sources), "vector_k": int(vector_k), "fts_k": int(fts_k)})
            return {"candidates": [], "ranks": {}, "counts": {"union": 0}}

        try:
            vector_rag_service._is_postgres = lambda: True  # type: ignore[assignment]
            vector_rag_service._pgvector_hybrid_fetch = _fake_fetch  # type: ignore[assignment]
            settings.vector_overfiltering_enabled = False

            out = vector_rag_service._pgvector_hybrid_query(
                project_id="p1", query_text="hello", query_vec=[0.1], sources=["outline"]
            )
            overfilter = out.get("overfilter")
            self.assertIsInstance(overfilter, dict)
            self.assertEqual(overfilter.get("actions"), [])
            self.assertEqual(len(calls), 1)
        finally:
            vector_rag_service._is_postgres = orig_is_postgres  # type: ignore[assignment]
            vector_rag_service._pgvector_hybrid_fetch = orig_fetch  # type: ignore[assignment]
            settings.vector_overfiltering_enabled = orig_overfilter
