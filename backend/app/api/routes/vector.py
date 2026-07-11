from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from app.api.deps import DbDep, UserIdDep, require_project_editor, require_project_owner, require_project_viewer
from app.core.errors import AppError, ok_payload
from app.core.secrets import redact_api_keys
from app.db.utils import utc_now
from app.models.knowledge_base import KnowledgeBase
from app.models.project_settings import ProjectSettings
from app.services.embedding_service import embed_texts, resolve_embedding_config
from app.services.memory_query_service import normalize_query_text, parse_query_preprocessing_config
from app.services.vector_embedding_overrides import vector_embedding_overrides
from app.services.vector_rerank_overrides import vector_rerank_overrides
from app.services.vector_kb_service import create_kb as create_vector_kb
from app.services.vector_kb_service import delete_kb as delete_vector_kb
from app.services.vector_kb_service import ensure_default_kb as ensure_default_vector_kb
from app.services.vector_kb_service import get_kb as get_vector_kb
from app.services.vector_kb_service import list_kbs as list_vector_kbs
from app.services.vector_kb_service import resolve_query_kbs as resolve_vector_query_kbs
from app.services.vector_kb_service import reorder_kbs as reorder_vector_kbs
from app.services.vector_kb_service import update_kb as update_vector_kb
from app.services.vector_rag_service import (
    _rerank_candidates as rerank_candidates,
    _resolve_rerank_config as resolve_rerank_config,
    _resolve_rerank_external_config as resolve_rerank_external_config,
)
from app.services.vector_rag_service import (
    VectorSource,
    build_project_chunks,
    ingest_chunks,
    purge_project_vectors,
    query_project,
    rebuild_project,
    vector_rag_status,
)

router = APIRouter()


def _ensure_settings_row(db, *, project_id: str) -> ProjectSettings:
    row = db.get(ProjectSettings, project_id)
    if row is None:
        row = ProjectSettings(project_id=project_id)
        db.add(row)
        db.flush()
    return row


def _index_state(row: ProjectSettings | None) -> dict[str, object]:
    if row is None:
        return {"dirty": False, "last_build_at": None}
    last_build_at = getattr(row, "last_vector_build_at", None)
    return {
        "dirty": bool(getattr(row, "vector_index_dirty", False)),
        "last_build_at": last_build_at.isoformat() if last_build_at else None,
    }


def _vector_rerank_config(row: ProjectSettings | None) -> dict[str, object]:
    return vector_rerank_overrides(row)


def _kb_public(row: KnowledgeBase) -> dict[str, object]:
    created_at = getattr(row, "created_at", None)
    updated_at = getattr(row, "updated_at", None)
    return {
        "kb_id": row.kb_id,
        "name": row.name,
        "enabled": bool(row.enabled),
        "weight": float(row.weight),
        "order": int(row.order_index),
        "priority_group": str(getattr(row, "priority_group", "normal") or "normal"),
        "created_at": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


class VectorIngestRequest(BaseModel):
    kb_id: str | None = Field(default=None, max_length=64)
    kb_ids: list[str] = Field(default_factory=list, max_length=200)
    sources: list[VectorSource] = Field(default_factory=lambda: ["outline", "chapter", "story_memory"], max_length=10)


class VectorQueryRequest(BaseModel):
    query_text: str = Field(default="", max_length=8000)
    kb_id: str | None = Field(default=None, max_length=64)
    kb_ids: list[str] = Field(default_factory=list, max_length=200)
    sources: list[VectorSource] = Field(default_factory=lambda: ["outline", "chapter", "story_memory"], max_length=10)
    rerank_hybrid_alpha: float | None = Field(default=None, ge=0.0, le=1.0)
    super_sort: dict[str, Any] | None = Field(default=None)


class VectorStatusRequest(BaseModel):
    kb_id: str | None = Field(default=None, max_length=64)
    sources: list[VectorSource] = Field(default_factory=lambda: ["outline", "chapter", "story_memory"], max_length=10)


class VectorEmbeddingDryRunRequest(BaseModel):
    text: str = Field(default="hello", max_length=8000)


class VectorRerankDryRunRequest(BaseModel):
    query_text: str = Field(default="", max_length=8000)
    documents: list[str] = Field(default_factory=list, max_length=50)
    method: str | None = Field(default=None, max_length=64)
    top_k: int | None = Field(default=None, ge=1, le=1000)
    hybrid_alpha: float | None = Field(default=None, ge=0.0, le=1.0)


class VectorKbCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kb_id: str | None = Field(default=None, max_length=64)
    enabled: bool = Field(default=True)
    weight: float = Field(default=1.0)
    priority_group: str | None = Field(default=None, max_length=16)


class VectorKbUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    enabled: bool | None = Field(default=None)
    weight: float | None = Field(default=None)
    priority_group: str | None = Field(default=None, max_length=16)


class VectorKbReorderRequest(BaseModel):
    kb_ids: list[str] = Field(default_factory=list, max_length=200)


@router.post("/projects/{project_id}/vector/status")
def get_vector_status(
    request: Request, db: DbDep, user_id: UserIdDep, project_id: str, body: VectorStatusRequest
) -> dict:
    request_id = request.state.request_id

    require_project_viewer(db, project_id=project_id, user_id=user_id)
    settings_row = db.get(ProjectSettings, project_id)
    embedding = vector_embedding_overrides(settings_row)
    rerank = _vector_rerank_config(settings_row)
    index_state = _index_state(settings_row)

    result = vector_rag_status(project_id=project_id, sources=body.sources, embedding=embedding, rerank=rerank)
    result["index"] = index_state
    return ok_payload(request_id=request_id, data=redact_api_keys({"result": result}))


@router.post("/projects/{project_id}/vector/embeddings/dry-run")
def dry_run_vector_embeddings(
    request: Request, db: DbDep, user_id: UserIdDep, project_id: str, body: VectorEmbeddingDryRunRequest
) -> dict:
    request_id = request.state.request_id
    text = str(body.text or "").strip()
    if not text:
        raise AppError.validation("text 不能为空")

    require_project_editor(db, project_id=project_id, user_id=user_id)
    settings_row = db.get(ProjectSettings, project_id)
    embedding = vector_embedding_overrides(settings_row)

    cfg = resolve_embedding_config(embedding)

    start = time.perf_counter()
    out = embed_texts([text], embedding=embedding)
    elapsed_ms = int((time.perf_counter() - start) * 1000)

    vectors = out.get("vectors") if isinstance(out.get("vectors"), list) else []
    dims: int | None = None
    if vectors and isinstance(vectors[0], list):
        dims = len(vectors[0])

    error = str(out.get("error") or "").strip() or None
    api_key = str(embedding.get("api_key") or "").strip()
    if api_key and error and api_key in error:
        error = error.replace(api_key, "[REDACTED]")

    result = {
        "enabled": bool(out.get("enabled")),
        "disabled_reason": out.get("disabled_reason"),
        "provider": cfg.provider,
        "dims": dims,
        "timings_ms": {"total": int(elapsed_ms)},
        "error": error,
        "embedding": cfg.model_dump(),
    }
    return ok_payload(request_id=request_id, data=redact_api_keys({"result": result}))


@router.post("/projects/{project_id}/vector/rerank/dry-run")
def dry_run_vector_rerank(
    request: Request, db: DbDep, user_id: UserIdDep, project_id: str, body: VectorRerankDryRunRequest
) -> dict:
    request_id = request.state.request_id
    query_text = str(body.query_text or "").strip()
    if not query_text:
        raise AppError.validation("query_text 不能为空")

    raw_docs = body.documents or []
    docs: list[str] = []
    for raw in raw_docs:
        doc = str(raw or "").strip()
        if not doc:
            raise AppError.validation("documents 不能包含空文本")
        docs.append(doc)
    if not docs:
        raise AppError.validation("documents 不能为空")

    require_project_editor(db, project_id=project_id, user_id=user_id)
    settings_row = db.get(ProjectSettings, project_id)
    rerank = _vector_rerank_config(settings_row)

    rerank_runtime: dict[str, Any] = dict(rerank or {})
    if body.method is not None:
        rerank_runtime["method"] = str(body.method or "").strip() or "auto"
    if body.top_k is not None:
        rerank_runtime["top_k"] = int(body.top_k)
    if body.hybrid_alpha is not None:
        rerank_runtime["hybrid_alpha"] = float(body.hybrid_alpha)

    start = time.perf_counter()
    rerank_enabled, rerank_method, rerank_top_k, rerank_hybrid_alpha = resolve_rerank_config(rerank_runtime)
    rerank_external = resolve_rerank_external_config(rerank_runtime)

    before_ids = [str(i) for i in range(len(docs))]
    obs: dict[str, Any] = {
        "enabled": bool(rerank_enabled),
        "applied": False,
        "requested_method": rerank_method,
        "method": None,
        "provider": None,
        "model": None,
        "top_k": int(rerank_top_k),
        "hybrid_alpha": float(rerank_hybrid_alpha),
        "hybrid_applied": False,
        "after_rerank": list(before_ids),
        "reason": "disabled",
        "error_type": None,
        "before": list(before_ids),
        "after": list(before_ids),
        "timing_ms": 0,
        "errors": [],
    }
    after_ids = list(before_ids)
    if rerank_enabled:
        candidates = [{"id": str(i), "text": doc, "metadata": {}} for i, doc in enumerate(docs)]
        reranked, obs = rerank_candidates(
            query_text=query_text,
            candidates=candidates,
            method=rerank_method,
            top_k=rerank_top_k,
            hybrid_alpha=rerank_hybrid_alpha,
            external=rerank_external,
        )
        after_ids = [str(c.get("id") or "") for c in reranked if isinstance(c, dict)]

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    order: list[int] = []
    for cid in after_ids:
        try:
            order.append(int(cid))
        except Exception:
            continue

    result = {
        "enabled": bool(rerank_enabled),
        "documents_count": int(len(docs)),
        "method": rerank_method,
        "top_k": int(rerank_top_k),
        "hybrid_alpha": float(rerank_hybrid_alpha),
        "order": order,
        "obs": obs,
        "timings_ms": {"total": int(elapsed_ms)},
        "rerank": rerank_runtime,
    }
    return ok_payload(request_id=request_id, data=redact_api_keys({"result": result}))


@router.post("/projects/{project_id}/vector/ingest")
def ingest_vector_index(
    request: Request, db: DbDep, user_id: UserIdDep, project_id: str, body: VectorIngestRequest
) -> dict:
    request_id = request.state.request_id

    kb_id = str(body.kb_id or "").strip() or None
    kb_ids = [str(x or "").strip() for x in (body.kb_ids or []) if str(x or "").strip()]
    kb_ids_unique: list[str] = []
    seen: set[str] = set()
    for kid in kb_ids:
        if kid in seen:
            continue
        seen.add(kid)
        kb_ids_unique.append(kid)
    if not kb_ids_unique:
        kb_ids_unique = [kb_id] if kb_id else ["default"]

    require_project_editor(db, project_id=project_id, user_id=user_id)
    chunks = build_project_chunks(db=db, project_id=project_id, sources=body.sources)
    embedding = vector_embedding_overrides(db.get(ProjectSettings, project_id))
    ensure_default_vector_kb(db, project_id=project_id)
    for kid in kb_ids_unique:
        get_vector_kb(db, project_id=project_id, kb_id=kid)

    per_kb: dict[str, dict] = {}
    for kid in kb_ids_unique:
        per_kb[kid] = ingest_chunks(project_id=project_id, kb_id=kid, chunks=chunks, embedding=embedding)

    results = list(per_kb.values())
    enabled = all(bool(r.get("enabled")) for r in results) if results else False
    skipped = all(bool(r.get("skipped")) for r in results) if results else True
    ingested = sum(int(r.get("ingested") or 0) for r in results)
    disabled_reason = next((r.get("disabled_reason") for r in results if r.get("disabled_reason")), None)
    backend = next((r.get("backend") for r in results if r.get("backend")), None)
    error = next((r.get("error") for r in results if r.get("error")), None)

    result = {
        "enabled": bool(enabled),
        "skipped": bool(skipped),
        "disabled_reason": disabled_reason,
        "ingested": int(ingested),
        "backend": backend,
        "error": error,
        "kbs": {"selected": list(kb_ids_unique), "per_kb": per_kb},
    }
    return ok_payload(request_id=request_id, data={"result": result})


@router.post("/projects/{project_id}/vector/rebuild")
def rebuild_vector_index(
    request: Request, db: DbDep, user_id: UserIdDep, project_id: str, body: VectorIngestRequest
) -> dict:
    request_id = request.state.request_id

    kb_id = str(body.kb_id or "").strip() or None
    kb_ids = [str(x or "").strip() for x in (body.kb_ids or []) if str(x or "").strip()]
    kb_ids_unique: list[str] = []
    seen: set[str] = set()
    for kid in kb_ids:
        if kid in seen:
            continue
        seen.add(kid)
        kb_ids_unique.append(kid)
    if not kb_ids_unique:
        kb_ids_unique = [kb_id] if kb_id else ["default"]

    require_project_editor(db, project_id=project_id, user_id=user_id)
    settings_row = _ensure_settings_row(db, project_id=project_id)
    db.commit()
    build_revision = int(settings_row.vector_dirty_revision)
    chunks = build_project_chunks(db=db, project_id=project_id, sources=body.sources)
    embedding = vector_embedding_overrides(settings_row)
    ensure_default_vector_kb(db, project_id=project_id)
    for kid in kb_ids_unique:
        get_vector_kb(db, project_id=project_id, kb_id=kid)

    per_kb: dict[str, dict] = {}
    for kid in kb_ids_unique:
        per_kb[kid] = rebuild_project(project_id=project_id, kb_id=kid, chunks=chunks, embedding=embedding)

    results = list(per_kb.values())
    enabled = all(bool(r.get("enabled")) for r in results) if results else False
    skipped = all(bool(r.get("skipped")) for r in results) if results else True
    rebuilt = sum(int(r.get("rebuilt") or 0) for r in results)
    disabled_reason = next((r.get("disabled_reason") for r in results if r.get("disabled_reason")), None)
    backend = next((r.get("backend") for r in results if r.get("backend")), None)
    error = next((r.get("error") for r in results if r.get("error")), None)

    result = {
        "enabled": bool(enabled),
        "skipped": bool(skipped),
        "disabled_reason": disabled_reason,
        "rebuilt": int(rebuilt),
        "backend": backend,
        "error": error,
        "kbs": {"selected": list(kb_ids_unique), "per_kb": per_kb},
    }

    if bool(enabled) and not bool(skipped):
        cleared = db.execute(
            update(ProjectSettings)
            .where(
                ProjectSettings.project_id == project_id,
                ProjectSettings.vector_dirty_revision == build_revision,
            )
            .values(vector_index_dirty=False, last_vector_build_at=utc_now())
        )
        db.commit()
        revision_matched = bool(getattr(cleared, "rowcount", 0))
        result.update({"stale": not revision_matched, "degraded": not revision_matched})
        if not revision_matched:
            current_revision = db.execute(
                select(ProjectSettings.vector_dirty_revision).where(ProjectSettings.project_id == project_id)
            ).scalar_one_or_none()
            result["current_dirty_revision"] = int(current_revision or 0)
    return ok_payload(request_id=request_id, data={"result": result})


@router.post("/projects/{project_id}/vector/purge")
def purge_vector_index(request: Request, db: DbDep, user_id: UserIdDep, project_id: str) -> dict:
    request_id = request.state.request_id

    require_project_owner(db, project_id=project_id, user_id=user_id)

    result = purge_project_vectors(project_id=project_id)
    return ok_payload(request_id=request_id, data={"result": result})


@router.post("/projects/{project_id}/vector/query")
def query_vector_index(
    request: Request, db: DbDep, user_id: UserIdDep, project_id: str, body: VectorQueryRequest
) -> dict:
    request_id = request.state.request_id

    kb_id = str(body.kb_id or "").strip() or None
    kb_ids = [str(x or "").strip() for x in (body.kb_ids or []) if str(x or "").strip()]
    kb_ids_unique: list[str] = []
    seen: set[str] = set()
    for kid in kb_ids:
        if kid in seen:
            continue
        seen.add(kid)
        kb_ids_unique.append(kid)
    requested_kb_ids = kb_ids_unique if kb_ids_unique else ([kb_id] if kb_id else None)

    require_project_viewer(db, project_id=project_id, user_id=user_id)
    settings_row = db.get(ProjectSettings, project_id)
    embedding = vector_embedding_overrides(settings_row)
    rerank = _vector_rerank_config(settings_row)
    selected_kbs = resolve_vector_query_kbs(db, project_id=project_id, requested_kb_ids=requested_kb_ids)
    qp_cfg = parse_query_preprocessing_config(
        (settings_row.query_preprocessing_json or "").strip() if settings_row is not None else None
    )

    selected_kb_ids = [r.kb_id for r in selected_kbs]
    kb_weights = {r.kb_id: float(r.weight) for r in selected_kbs}
    kb_orders = {r.kb_id: int(r.order_index) for r in selected_kbs}
    kb_priority_groups = {r.kb_id: str(getattr(r, "priority_group", "normal") or "normal") for r in selected_kbs}

    normalized, preprocess_obs = normalize_query_text(query_text=body.query_text, config=qp_cfg)
    if body.rerank_hybrid_alpha is not None:
        rerank = dict(rerank)
        rerank["hybrid_alpha"] = float(body.rerank_hybrid_alpha)
    result = query_project(
        project_id=project_id,
        kb_ids=selected_kb_ids,
        query_text=normalized,
        sources=body.sources,
        embedding=embedding,
        rerank=rerank,
        super_sort=body.super_sort,
        kb_weights=kb_weights,
        kb_orders=kb_orders,
        kb_priority_groups=kb_priority_groups,
    )
    return ok_payload(
        request_id=request_id,
        data=redact_api_keys(
            {
                "result": result,
                "raw_query_text": body.query_text,
                "normalized_query_text": normalized,
                "preprocess_obs": preprocess_obs,
            }
        ),
    )


@router.get("/projects/{project_id}/vector/kbs")
def list_vector_knowledge_bases(request: Request, db: DbDep, user_id: UserIdDep, project_id: str) -> dict:
    request_id = request.state.request_id

    require_project_viewer(db, project_id=project_id, user_id=user_id)
    rows = list_vector_kbs(db, project_id=project_id)
    return ok_payload(request_id=request_id, data={"kbs": [_kb_public(r) for r in rows]})


@router.post("/projects/{project_id}/vector/kbs")
def create_vector_knowledge_base(
    request: Request, db: DbDep, user_id: UserIdDep, project_id: str, body: VectorKbCreateRequest
) -> dict:
    request_id = request.state.request_id

    require_project_editor(db, project_id=project_id, user_id=user_id)
    row = create_vector_kb(
        db,
        project_id=project_id,
        name=body.name,
        kb_id=body.kb_id,
        enabled=body.enabled,
        weight=body.weight,
        priority_group=body.priority_group,
    )
    return ok_payload(request_id=request_id, data={"kb": _kb_public(row)})


@router.put("/projects/{project_id}/vector/kbs/{kb_id}")
def update_vector_knowledge_base(
    request: Request, db: DbDep, user_id: UserIdDep, project_id: str, kb_id: str, body: VectorKbUpdateRequest
) -> dict:
    request_id = request.state.request_id
    kb = str(kb_id or "").strip()
    if not kb:
        raise AppError.validation("kb_id 不能为空")

    require_project_editor(db, project_id=project_id, user_id=user_id)
    row = update_vector_kb(
        db,
        project_id=project_id,
        kb_id=kb,
        name=body.name,
        enabled=body.enabled,
        weight=body.weight,
        priority_group=body.priority_group,
    )
    return ok_payload(request_id=request_id, data={"kb": _kb_public(row)})


@router.post("/projects/{project_id}/vector/kbs/reorder")
def reorder_vector_knowledge_bases(
    request: Request, db: DbDep, user_id: UserIdDep, project_id: str, body: VectorKbReorderRequest
) -> dict:
    request_id = request.state.request_id

    require_project_editor(db, project_id=project_id, user_id=user_id)
    rows = reorder_vector_kbs(db, project_id=project_id, ordered_kb_ids=body.kb_ids)
    return ok_payload(request_id=request_id, data={"kbs": [_kb_public(r) for r in rows]})


@router.delete("/projects/{project_id}/vector/kbs/{kb_id}")
def delete_vector_knowledge_base(request: Request, db: DbDep, user_id: UserIdDep, project_id: str, kb_id: str) -> dict:
    request_id = request.state.request_id
    kb = str(kb_id or "").strip()
    if not kb:
        raise AppError.validation("kb_id 不能为空")

    require_project_owner(db, project_id=project_id, user_id=user_id)
    kb_row = get_vector_kb(db, project_id=project_id, kb_id=kb)
    if kb == "default":
        raise AppError.validation("default KB 不可删除")
    if bool(kb_row.enabled):
        raise AppError.validation("请先禁用 KB 再删除")
    purge_out = purge_project_vectors(project_id=project_id, kb_id=kb)
    if not bool(purge_out.get("deleted")):
        raise AppError.conflict(
            "KB 向量数据清理失败，未删除 KB",
            details={
                "kb_id": kb,
                "backend": purge_out.get("backend"),
                "error_type": purge_out.get("error_type"),
            },
        )
    delete_vector_kb(db, project_id=project_id, kb_id=kb)
    return ok_payload(request_id=request_id, data={"deleted": True, "vector_purge": purge_out})
