from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProjectSettings(Base):
    __tablename__ = "project_settings"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    world_setting: Mapped[str | None] = mapped_column(Text, nullable=True)
    style_guide: Mapped[str | None] = mapped_column(Text, nullable=True)
    constraints: Mapped[str | None] = mapped_column(Text, nullable=True)

    vector_embedding_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vector_embedding_base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    vector_embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vector_embedding_azure_deployment: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vector_embedding_azure_api_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vector_embedding_sentence_transformers_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vector_embedding_api_key_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    vector_embedding_api_key_masked: Mapped[str | None] = mapped_column(String(64), nullable=True)

    vector_rerank_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    vector_rerank_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vector_rerank_top_k: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vector_rerank_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vector_rerank_base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    vector_rerank_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vector_rerank_api_key_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    vector_rerank_api_key_masked: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vector_rerank_timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vector_rerank_hybrid_alpha: Mapped[float | None] = mapped_column(Float, nullable=True)

    query_preprocessing_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    context_optimizer_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Auto update switches (default ON for normal users).
    #
    # Keep this list aligned with Alembic revision e1c65f9a82c6. Production
    # databases already contain these NOT NULL columns; if the ORM model omits a
    # column, SQLAlchemy will generate INSERTs that violate the database schema
    # when project_settings rows are lazily created at runtime.
    auto_update_worldbook_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_update_characters_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_update_story_memory_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_update_graph_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_update_vector_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_update_search_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_update_fractal_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_update_tables_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    vector_index_dirty: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_vector_build_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None, nullable=True)
