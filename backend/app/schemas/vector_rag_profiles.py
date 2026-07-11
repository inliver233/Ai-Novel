from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.base import RequestModel


class VectorRagProfileCreate(RequestModel):
    name: str = Field(min_length=1, max_length=255)
    vector_embedding_provider: str | None = Field(default=None, max_length=64)
    vector_embedding_base_url: str | None = Field(default=None, max_length=2048)
    vector_embedding_model: str | None = Field(default=None, max_length=255)
    vector_embedding_expected_dimension: int = Field(default=1536, ge=1, le=65535)
    vector_embedding_api_key: str | None = Field(default=None, max_length=4096)
    vector_rerank_provider: str | None = Field(default=None, max_length=64)
    vector_rerank_base_url: str | None = Field(default=None, max_length=2048)
    vector_rerank_model: str | None = Field(default=None, max_length=255)
    vector_rerank_api_key: str | None = Field(default=None, max_length=4096)


class VectorRagProfileUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    vector_embedding_provider: str | None = Field(default=None, max_length=64)
    vector_embedding_base_url: str | None = Field(default=None, max_length=2048)
    vector_embedding_model: str | None = Field(default=None, max_length=255)
    vector_embedding_expected_dimension: int | None = Field(default=None, ge=1, le=65535)
    vector_embedding_api_key: str | None = Field(default=None, max_length=4096)
    vector_rerank_provider: str | None = Field(default=None, max_length=64)
    vector_rerank_base_url: str | None = Field(default=None, max_length=2048)
    vector_rerank_model: str | None = Field(default=None, max_length=255)
    vector_rerank_api_key: str | None = Field(default=None, max_length=4096)


class VectorRagProfileOut(BaseModel):
    id: str
    owner_user_id: str
    name: str
    vector_embedding_provider: str | None = None
    vector_embedding_base_url: str | None = None
    vector_embedding_model: str | None = None
    vector_embedding_expected_dimension: int = 1536
    vector_embedding_has_api_key: bool = False
    vector_embedding_masked_api_key: str | None = None
    vector_rerank_provider: str | None = None
    vector_rerank_base_url: str | None = None
    vector_rerank_model: str | None = None
    vector_rerank_has_api_key: bool = False
    vector_rerank_masked_api_key: str | None = None
    created_at: datetime
    updated_at: datetime
