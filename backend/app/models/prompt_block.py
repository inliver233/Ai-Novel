from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.utils import utc_now


class PromptBlock(Base):
    __tablename__ = "prompt_blocks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    preset_id: Mapped[str] = mapped_column(ForeignKey("prompt_presets.id", ondelete="CASCADE"), nullable=False)
    identifier: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    template: Mapped[str | None] = mapped_column(Text, nullable=True)
    marker_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    injection_position: Mapped[str] = mapped_column(String(16), nullable=False, default="relative")
    injection_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    injection_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    triggers_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    forbid_overrides: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    budget_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    cache_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_template_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_template_outdated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


Index("ix_prompt_blocks_preset_id", PromptBlock.preset_id)
