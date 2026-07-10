from __future__ import annotations

import unittest

from app.services import vector_rag_service, vector_rerank


class TestVectorRerank(unittest.TestCase):
    def test_rerank_prefers_more_relevant_candidate(self) -> None:
        candidates = [
            {"id": "b", "text": "apple banana", "metadata": {}},
            {"id": "a", "text": "dragon castle", "metadata": {}},
        ]

        reranked, obs = vector_rag_service._rerank_candidates(
            query_text="dragon castle",
            candidates=candidates,
            method="auto",
            top_k=20,
        )
        self.assertIsInstance(reranked, list)
        self.assertEqual([c.get("id") for c in reranked], ["a", "b"])
        self.assertTrue(obs.get("enabled"))
        self.assertTrue(obs.get("applied"))
        self.assertEqual(obs.get("before"), ["b", "a"])
        self.assertEqual(obs.get("after"), ["a", "b"])
        self.assertIsInstance(obs.get("timing_ms"), int)

    def test_rerank_failsoft_when_scoring_raises(self) -> None:
        orig = vector_rerank._rerank_score

        def _boom(*, method: str, query_text: str, candidate_text: str) -> float:  # pragma: no cover
            raise RuntimeError("boom")

        try:
            vector_rerank._rerank_score = _boom  # type: ignore[assignment]
            candidates = [
                {"id": "x", "text": "dragon", "metadata": {}},
                {"id": "y", "text": "castle", "metadata": {}},
            ]
            reranked, obs = vector_rag_service._rerank_candidates(
                query_text="dragon castle",
                candidates=candidates,
                method="auto",
                top_k=20,
            )
            self.assertEqual([c.get("id") for c in reranked], ["x", "y"])
            self.assertTrue(obs.get("enabled"))
            self.assertFalse(obs.get("applied"))
            self.assertEqual(obs.get("before"), ["x", "y"])
            self.assertEqual(obs.get("after"), ["x", "y"])
            self.assertIsInstance(obs.get("errors"), list)
            self.assertGreaterEqual(len(obs.get("errors") or []), 1)
        finally:
            vector_rerank._rerank_score = orig  # type: ignore[assignment]

    def test_rerank_hybrid_alpha_preserves_original_order(self) -> None:
        candidates = [
            {"id": "b", "text": "apple banana", "metadata": {}},
            {"id": "a", "text": "dragon castle", "metadata": {}},
        ]

        reranked, obs = vector_rag_service._rerank_candidates(
            query_text="dragon castle",
            candidates=candidates,
            method="auto",
            top_k=20,
            hybrid_alpha=1.0,
        )
        self.assertEqual([c.get("id") for c in reranked], ["b", "a"])
        self.assertEqual(obs.get("after_rerank"), ["a", "b"])
        self.assertTrue(bool(obs.get("hybrid_applied")))
        self.assertFalse(bool(obs.get("applied")))
