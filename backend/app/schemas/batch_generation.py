from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.base import ORMModel
from app.schemas.chapter_generate import ChapterGenerateContext


BatchGenerationTaskStatus = Literal["queued", "running", "paused", "succeeded", "failed", "canceled"]
BatchGenerationItemStatus = Literal["queued", "running", "succeeded", "failed", "canceled", "skipped"]


class BatchGenerationCreateRequest(BaseModel):
    after_chapter_id: str | None = Field(default=None, max_length=36)
    count: int = Field(ge=1, le=200)
    include_existing: bool = False
    instruction: str = Field(default="", max_length=4000)
    target_word_count: int | None = Field(default=None, ge=100, le=50000)
    plan_first: bool = False
    post_edit: bool = False
    post_edit_sanitize: bool = False
    content_optimize: bool = False
    style_id: str | None = Field(default=None, max_length=36)
    context: ChapterGenerateContext = Field(default_factory=ChapterGenerateContext)


class BatchGenerationTaskItemOut(ORMModel):
    id: str
    task_id: str
    chapter_id: str | None = None
    chapter_number: int
    status: BatchGenerationItemStatus
    attempt_count: int
    generation_run_id: str | None = None
    last_request_id: str | None = None
    error_message: str | None = None
    last_error_json: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class BatchGenerationTaskOut(ORMModel):
    id: str
    project_id: str
    outline_id: str
    actor_user_id: str | None = None
    project_task_id: str | None = None
    runtime_provider: str | None = None
    status: BatchGenerationTaskStatus
    total_count: int
    completed_count: int
    failed_count: int
    skipped_count: int
    cancel_requested: bool
    pause_requested: bool
    checkpoint_json: str | None = None
    error_json: str | None = None
    created_at: datetime
    updated_at: datetime
