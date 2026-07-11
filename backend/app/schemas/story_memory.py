from __future__ import annotations

from pydantic import Field

from app.schemas.base import RequestModel


class StoryMemoryCreateRequest(RequestModel):
    chapter_id: str | None = Field(default=None, max_length=36)
    memory_type: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=255)
    content: str = Field(min_length=1, max_length=20000)
    full_context_md: str | None = Field(default=None, max_length=40000)
    importance_score: float = Field(default=0.0)
    tags: list[str] = Field(default_factory=list, max_length=80)
    story_timeline: int = Field(default=0)
    text_position: int = Field(default=-1)
    text_length: int = Field(default=0, ge=0)
    is_foreshadow: bool = Field(default=False)


class StoryMemoryUpdateRequest(RequestModel):
    chapter_id: str | None = Field(default=None, max_length=36)
    memory_type: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=255)
    content: str | None = Field(default=None, max_length=20000)
    full_context_md: str | None = Field(default=None, max_length=40000)
    importance_score: float | None = None
    tags: list[str] | None = Field(default=None, max_length=80)
    story_timeline: int | None = None
    text_position: int | None = None
    text_length: int | None = Field(default=None, ge=0)
    is_foreshadow: bool | None = None


class StoryMemoryMergeRequest(RequestModel):
    target_id: str = Field(max_length=36)
    source_ids: list[str] = Field(default_factory=list, min_length=1, max_length=20)


class StoryMemoryMarkDoneRequest(RequestModel):
    done: bool = True
