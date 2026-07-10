from __future__ import annotations

"""Thin re-export hub for vector RAG.

Keep importing from `app.services.vector_rag_service` to avoid churn in callers.
Implementation is split into focused modules:
- `vector_build.py`: backends + chunk build/ingest/rebuild/purge
- `vector_retrieval.py`: query/status + retrieval helpers
- `vector_rerank.py`: rerank scoring + rerank config
"""

from app.services.vector_build import (
    VectorChunk,
    VectorSource,
    _ALL_SOURCES,
    _default_chroma_persist_dir,
    _vector_enabled_reason,
    schedule_vector_rebuild_task,
)

# IMPORTANT: these MUST stay importable from `app.services.vector_rag_service`
# for backward-compat (external modules import them from here).
_PGVECTOR_TABLE = "vector_chunks"
_PGVECTOR_READY_CACHE: tuple[bool, float] | None = None
_PGVECTOR_READY_CACHE_TTL_SECONDS = 30.0
_VECTOR_DROPPED_REASON_EXPLAIN = {
    "duplicate_chunk": "同一 source/source_id/chunk_index 已存在于最终候选，避免重复注入。",
    "per_source_budget": "同一 source+source_id 的 chunk 数达到上限（vector_per_source_id_max_chunks）。",
    "budget": "达到最终注入 chunk 上限（vector_final_max_chunks）。",
}

from app.services.vector_storage import (
    _is_postgres, _pgvector_ready, _prefer_pgvector, _pgvector_literal, _safe_json_loads,
    _rrf_contrib, _rrf_score, _import_chromadb, _cosine_distance, _InMemoryCollection,
    _InMemoryClient, _InMemoryChromaModule, _normalize_kb_id, _legacy_collection_name,
    _hash_collection_name, _chroma_collection_naming, _migrate_chroma_collection,
    _get_collection, _pgvector_upsert_chunks, _pgvector_delete_project,
    _pgvector_hybrid_fetch, _pgvector_hybrid_query, ingest_chunks, rebuild_project,
    purge_project_vectors,
)
from app.services.vector_chunk_builder import _chunk_text, build_project_chunks
from app.services.vector_rerank import (
    _rerank_candidates,
    _rerank_score,
    _rerank_tokens,
    _resolve_rerank_config,
    _resolve_rerank_external_config,
)
from app.services.vector_retrieval import (
    _build_vector_query_counts,
    _merge_kb_candidates,
    _merge_kb_candidates_rrf,
    _normalize_kb_priority_group,
    _parse_vector_source_order,
    _parse_vector_source_weights,
    _super_sort_final_chunks,
    _vector_budget_observability,
    _vector_candidate_chunk_key,
    _vector_candidate_key,
    query_project,
    vector_rag_status,
)

__all__ = [
    "VectorChunk",
    "VectorSource",
    "_ALL_SOURCES",
    "_PGVECTOR_TABLE",
    "_PGVECTOR_READY_CACHE",
    "_PGVECTOR_READY_CACHE_TTL_SECONDS",
    "_VECTOR_DROPPED_REASON_EXPLAIN",
    "build_project_chunks",
    "ingest_chunks",
    "purge_project_vectors",
    "query_project",
    "rebuild_project",
    "schedule_vector_rebuild_task",
    "vector_rag_status",
    "_rerank_candidates",
    "_rerank_score",
    "_rerank_tokens",
    "_resolve_rerank_config",
    "_resolve_rerank_external_config",
    "_merge_kb_candidates",
    "_merge_kb_candidates_rrf",
    "_super_sort_final_chunks",
    "_build_vector_query_counts",
    "_vector_budget_observability",
    "_vector_candidate_key",
    "_vector_candidate_chunk_key",
    "_normalize_kb_priority_group",
    "_parse_vector_source_order",
    "_parse_vector_source_weights",
    "_is_postgres",
    "_pgvector_ready",
    "_prefer_pgvector",
    "_safe_json_loads",
    "_pgvector_literal",
    "_rrf_contrib",
    "_rrf_score",
    "_pgvector_upsert_chunks",
    "_pgvector_delete_project",
    "_pgvector_hybrid_fetch",
    "_pgvector_hybrid_query",
    "_import_chromadb",
    "_cosine_distance",
    "_InMemoryCollection",
    "_InMemoryClient",
    "_InMemoryChromaModule",
    "_normalize_kb_id",
    "_legacy_collection_name",
    "_hash_collection_name",
    "_chroma_collection_naming",
    "_migrate_chroma_collection",
    "_get_collection",
    "_chunk_text",
    "_vector_enabled_reason",
]
