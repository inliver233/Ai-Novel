"""Regression coverage for vector source-order string parsing."""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.services.vector_retrieval import (
    _parse_vector_source_order,
    _parse_vector_source_weights,
    _super_sort_final_chunks,
)


def test_source_order_parse_keeps_s_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "vector_source_order", "chapter,outline,story_memory")

    assert _parse_vector_source_order() == ["chapter", "outline", "story_memory"]


def test_source_order_parse_supports_whitespace_separator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "vector_source_order", "chapter outline\tstory_memory")

    assert _parse_vector_source_order() == ["chapter", "outline", "story_memory"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("chapter|outline;story_memory", ["chapter", "outline", "story_memory"]),
        ("INVALID, Story_Memory; chapter,story_memory", ["story_memory", "chapter"]),
        ("worldbook,chapter", ["chapter"]),
    ],
)
def test_source_order_parse_supports_other_separators_and_normalization(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: list[str],
) -> None:
    monkeypatch.setattr(settings, "vector_source_order", raw)

    assert _parse_vector_source_order() == expected


def test_source_weights_ignore_removed_worldbook_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "vector_source_weights_json", '{"worldbook": 9, "chapter": 2}')

    assert _parse_vector_source_weights() == {"chapter": 2.0}


def test_super_sort_string_override_keeps_story_memory_and_whitespace() -> None:
    chunks = [
        {"id": "o1", "metadata": {"source": "outline", "source_id": "o", "chunk_index": 0}},
        {"id": "c1", "metadata": {"source": "chapter", "source_id": "c", "chunk_index": 0}},
        {"id": "s1", "metadata": {"source": "story_memory", "source_id": "s", "chunk_index": 0}},
    ]

    sorted_chunks, observability = _super_sort_final_chunks(
        chunks,
        super_sort={"enabled": True, "source_order": "story_memory | chapter;\noutline"},
    )

    assert [chunk["id"] for chunk in sorted_chunks] == ["s1", "c1", "o1"]
    assert observability["source_order"] == ["story_memory", "chapter", "outline"]
    assert observability["source_order_effective"][:3] == ["story_memory", "chapter", "outline"]
