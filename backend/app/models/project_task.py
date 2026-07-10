from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.utils import utc_now


class TaskKind(str, Enum):
    BATCH_GENERATION = "batch_generation"
    BATCH_GENERATION_ORCHESTRATOR = "batch_generation_orchestrator"
    CHARACTERS_AUTO_UPDATE = "characters_auto_update"
    IMPORT_TASK = "import_task"
    NOOP = "noop"
    PROJECT_TASK = "project_task"
    SEARCH_REBUILD = "search_rebuild"
    VECTOR_REBUILD = "vector_rebuild"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"


class ProjectTask(Base):
    __tablename__ = "project_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    kind: Mapped[TaskKind] = mapped_column(String(64), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(String(16), nullable=False, default="queued")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    params_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_project_tasks_project_idempotency_key"),
    )


Index("ix_project_tasks_project_id", ProjectTask.project_id)
Index("ix_project_tasks_project_id_kind", ProjectTask.project_id, ProjectTask.kind)
Index("ix_project_tasks_project_id_status", ProjectTask.project_id, ProjectTask.status)
Index("ix_project_tasks_status", ProjectTask.status)
Index("ix_project_tasks_status_heartbeat_at", ProjectTask.status, ProjectTask.heartbeat_at)
