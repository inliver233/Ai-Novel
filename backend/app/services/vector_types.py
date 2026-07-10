from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

VectorSource = Literal["outline", "chapter", "story_memory"]


@dataclass(frozen=True, slots=True)
class VectorChunk:
    id: str
    text: str
    metadata: dict[str, Any]


_ALL_SOURCES: list[VectorSource] = ["outline", "chapter", "story_memory"]


__all__ = ["VectorChunk", "VectorSource", "_ALL_SOURCES"]
