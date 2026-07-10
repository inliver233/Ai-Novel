from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.utils import utc_now


class VectorRagProfile(Base):
    __tablename__ = "vector_rag_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Embedding fields
    vector_embedding_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vector_embedding_base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    vector_embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vector_embedding_api_key_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    vector_embedding_api_key_masked: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Rerank fields
    vector_rerank_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vector_rerank_base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    vector_rerank_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vector_rerank_api_key_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    vector_rerank_api_key_masked: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


Index("ix_vector_rag_profiles_owner_user_id", VectorRagProfile.owner_user_id)
