from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.utils import utc_now


class DetailedOutline(Base):
    __tablename__ = "detailed_outlines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    outline_id: Mapped[str] = mapped_column(ForeignKey("outlines.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    volume_number: Mapped[int] = mapped_column(Integer, nullable=False)
    volume_title: Mapped[str] = mapped_column(String(255), nullable=False)
    content_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    structure_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned", server_default="planned")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    __table_args__ = (UniqueConstraint("outline_id", "volume_number", name="uq_detailed_outlines_outline_volume"),)


Index("ix_detailed_outlines_outline_id", DetailedOutline.outline_id)
Index("ix_detailed_outlines_project_id", DetailedOutline.project_id)
