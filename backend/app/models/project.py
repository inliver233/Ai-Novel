from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.utils import utc_now


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    active_outline_id: Mapped[str | None] = mapped_column(
        ForeignKey("outlines.id", ondelete="SET NULL"),
        nullable=True,
    )
    llm_profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("llm_profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    genre: Mapped[str | None] = mapped_column(String(255), nullable=True)
    logline: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


Index("ix_projects_owner_user_id", Project.owner_user_id)
Index("ix_projects_active_outline_id", Project.active_outline_id)
Index("ix_projects_llm_profile_id", Project.llm_profile_id)
