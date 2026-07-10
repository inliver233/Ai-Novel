"""向量构建、检索与兼容 facade 共用同一套 lite 活跃 source 契约。"""

from __future__ import annotations

from typing import get_args

from app.services.vector_build import VectorSource, _ALL_SOURCES as BUILD_SOURCES
from app.services.vector_chunk_builder import _ALL_SOURCES as CHUNK_BUILDER_SOURCES
from app.services.vector_rag_service import _ALL_SOURCES as RAG_SERVICE_SOURCES
from app.services.vector_retrieval import _ALL_SOURCES as RETRIEVAL_SOURCES
from app.services.vector_storage import _ALL_SOURCES as STORAGE_SOURCES
from app.services.vector_types import _ALL_SOURCES as TYPE_SOURCES


def test_all_sources_consistent_across_modules() -> None:
    """所有公开兼容入口引用同一常量，且不重新引入已删除的 worldbook 源。"""
    canonical_sources = ["outline", "chapter", "story_memory"]

    assert BUILD_SOURCES == canonical_sources
    assert BUILD_SOURCES is TYPE_SOURCES
    assert RAG_SERVICE_SOURCES == canonical_sources
    assert CHUNK_BUILDER_SOURCES == canonical_sources
    assert BUILD_SOURCES is RAG_SERVICE_SOURCES
    assert BUILD_SOURCES is CHUNK_BUILDER_SOURCES
    assert BUILD_SOURCES is RETRIEVAL_SOURCES
    assert BUILD_SOURCES is STORAGE_SOURCES
    assert list(get_args(VectorSource)) == canonical_sources
