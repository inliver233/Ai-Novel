from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.utils import utc_now


class UserUsageStat(Base):
    __tablename__ = "user_usage_stats"

    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    total_generation_calls: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    total_generation_error_calls: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    total_generated_chars: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    last_generation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


Index("ix_user_usage_stats_last_generation_at", UserUsageStat.last_generation_at)
