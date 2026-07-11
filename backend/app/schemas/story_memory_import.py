from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.base import RequestModel

StoryMemoryImportSchemaVersion = Literal["story_memory_import_v1"]


class StoryMemoryImportV1Item(RequestModel):
    memory_type: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=255)
    content: str = Field(min_length=1, max_length=8000)
    importance_score: float = Field(default=0.0)
    story_timeline: int = Field(default=0)
    is_foreshadow: int = Field(default=0, ge=0, le=1)


class StoryMemoryImportV1Request(RequestModel):
    schema_version: StoryMemoryImportSchemaVersion = "story_memory_import_v1"
    memories: list[StoryMemoryImportV1Item] = Field(default_factory=list, min_length=1, max_length=50)
