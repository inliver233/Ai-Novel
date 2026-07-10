from __future__ import annotations

import unittest

from app.services.vector_rag_service import _super_sort_final_chunks


class TestVectorSuperSortOverride(unittest.TestCase):
    def test_super_sort_override_source_order(self) -> None:
        chunks = [
            {"id": "m1", "metadata": {"source": "story_memory", "source_id": "m", "chunk_index": 0, "title": "M"}},
            {"id": "c1", "metadata": {"source": "chapter", "source_id": "c", "chunk_index": 0, "chapter_number": 1}},
            {"id": "o1", "metadata": {"source": "outline", "source_id": "o", "chunk_index": 0, "title": "O"}},
        ]

        sorted_chunks, obs = _super_sort_final_chunks(
            chunks,
            super_sort={"enabled": True, "source_order": ["chapter", "story_memory", "outline"]},
        )

        self.assertEqual([c.get("id") for c in sorted_chunks], ["c1", "m1", "o1"])
        self.assertTrue(bool(obs.get("enabled")))
        self.assertTrue(bool(obs.get("applied")))

    def test_super_sort_override_can_disable(self) -> None:
        chunks = [
            {"id": "m1", "metadata": {"source": "story_memory", "source_id": "m", "chunk_index": 0, "title": "M"}},
            {"id": "c1", "metadata": {"source": "chapter", "source_id": "c", "chunk_index": 0, "chapter_number": 1}},
            {"id": "o1", "metadata": {"source": "outline", "source_id": "o", "chunk_index": 0, "title": "O"}},
        ]

        sorted_chunks, obs = _super_sort_final_chunks(
            chunks,
            super_sort={"enabled": False, "source_order": ["chapter", "story_memory", "outline"]},
        )

        self.assertEqual([c.get("id") for c in sorted_chunks], ["m1", "c1", "o1"])
        self.assertFalse(bool(obs.get("enabled")))
