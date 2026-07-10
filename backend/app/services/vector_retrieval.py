from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from app.core.config import settings
from app.core.logging import log_event
from app.services.context_budget_observability import build_budget_observability
from app.services.embedding_service import embed_texts as embed_texts_with_providers
from app.services.vector_storage import (
    _get_collection,
    _normalize_kb_id,
    _pgvector_hybrid_query,
    _prefer_pgvector,
    _rrf_contrib,
    _vector_enabled_reason,
)
from app.services.vector_types import VectorSource, _ALL_SOURCES
from app.services.vector_rerank import (
    _rerank_candidates,
    _resolve_rerank_config,
    _resolve_rerank_external_config,
)

logger = logging.getLogger("ainovel")

_VECTOR_DROPPED_REASON_EXPLAIN = {
    "duplicate_chunk": "同一 source/source_id/chunk_index 已存在于最终候选，避免重复注入。",
    "per_source_budget": "同一 source+source_id 的 chunk 数达到上限（vector_per_source_id_max_chunks）。",
    "budget": "达到最终注入 chunk 上限（vector_final_max_chunks）。",
}

def _vector_candidate_key(candidate: dict[str, Any]) -> tuple[str, str]:
    meta = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    return (str(meta.get("source") or ""), str(meta.get("source_id") or ""))


def _vector_candidate_chunk_key(candidate: dict[str, Any]) -> tuple[str, str, int]:
    meta = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    try:
        chunk_index = int(meta.get("chunk_index") or 0)
    except Exception:
        chunk_index = 0
    return (str(meta.get("source") or ""), str(meta.get("source_id") or ""), chunk_index)


def _normalize_kb_priority_group(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in ("normal", "high") else "normal"


def _merge_kb_candidates_rrf(
    *,
    kb_ids: list[str],
    per_kb_candidates: dict[str, list[dict[str, Any]]],
    kb_weights: dict[str, float],
    kb_orders: dict[str, int],
    rrf_k: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Merge multiple kb candidate lists using weighted RRF.

    Deterministic order:
      - score desc
      - kb_order asc
      - distance asc
      - id asc
    """

    merged_obs: dict[str, Any] = {"mode": "rrf", "kb_ids": list(kb_ids), "rrf_k": int(rrf_k)}
    if len(kb_ids) <= 1:
        only = kb_ids[0] if kb_ids else None
        merged = list(per_kb_candidates.get(str(only or "")) or []) if only else []
        merged_obs["mode"] = "single"
        merged_obs["candidate_count"] = int(len(merged))
        return merged, merged_obs

    scored: dict[str, dict[str, Any]] = {}
    for kid in kb_ids:
        cand_list = per_kb_candidates.get(kid) or []
        weight = float(kb_weights.get(kid, 1.0))
        kb_order = int(kb_orders.get(kid, 999))
        for rank, c in enumerate(cand_list, start=1):
            cid = str(c.get("id") or "")
            if not cid:
                continue
            contrib = float(weight) * _rrf_contrib(rank, k=rrf_k)
            dist = float(c.get("distance") or 0.0)
            entry = scored.get(cid)
            if entry is None:
                scored[cid] = {"candidate": c, "score": contrib, "distance": dist, "kb_order": kb_order, "id": cid}
            else:
                entry["score"] = float(entry.get("score") or 0.0) + contrib
                prev_dist = entry.get("distance")
                if prev_dist is None or dist < float(prev_dist):
                    entry["candidate"] = c
                    entry["distance"] = dist
                prev_order = entry.get("kb_order")
                entry["kb_order"] = min(int(prev_order) if prev_order is not None else kb_order, kb_order)
    merged = list(scored.values())
    merged.sort(
        key=lambda x: (
            -float(x.get("score") or 0.0),
            int(x.get("kb_order")) if x.get("kb_order") is not None else 999,
            float(x.get("distance")) if x.get("distance") is not None else 0.0,
            str(x.get("id") or ""),
        )
    )
    candidates = [dict(x.get("candidate") or {}) for x in merged]
    for c in candidates:
        c.pop("_rrf_score", None)

    merged_obs["candidate_count"] = int(len(candidates))
    return candidates, merged_obs


def _merge_kb_candidates(
    *,
    kb_ids: list[str],
    per_kb_candidates: dict[str, list[dict[str, Any]]],
    kb_weights: dict[str, float],
    kb_orders: dict[str, int],
    kb_priority_groups: dict[str, str],
    top_k: int,
    priority_enabled: bool,
    rrf_k: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not bool(priority_enabled):
        candidates, obs = _merge_kb_candidates_rrf(
            kb_ids=kb_ids,
            per_kb_candidates=per_kb_candidates,
            kb_weights=kb_weights,
            kb_orders=kb_orders,
            rrf_k=rrf_k,
        )
        obs["priority_enabled"] = False
        obs.setdefault("candidate_count", int(len(candidates)))
        return candidates, obs

    high_kb_ids = [kid for kid in kb_ids if kb_priority_groups.get(kid) == "high"]
    if not high_kb_ids:
        candidates, obs = _merge_kb_candidates_rrf(
            kb_ids=kb_ids,
            per_kb_candidates=per_kb_candidates,
            kb_weights=kb_weights,
            kb_orders=kb_orders,
            rrf_k=rrf_k,
        )
        obs["priority_enabled"] = True
        obs.setdefault("candidate_count", int(len(candidates)))
        obs.setdefault("note", "no_high_priority_kbs")
        return candidates, obs

    high_set = set(high_kb_ids)
    normal_kb_ids = [kid for kid in kb_ids if kid not in high_set]
    if not normal_kb_ids:
        candidates, obs = _merge_kb_candidates_rrf(
            kb_ids=kb_ids,
            per_kb_candidates=per_kb_candidates,
            kb_weights=kb_weights,
            kb_orders=kb_orders,
            rrf_k=rrf_k,
        )
        obs["priority_enabled"] = True
        obs.setdefault("candidate_count", int(len(candidates)))
        obs.setdefault("note", "only_high_priority_kbs")
        return candidates, obs

    high_candidates, high_obs = _merge_kb_candidates_rrf(
        kb_ids=high_kb_ids,
        per_kb_candidates=per_kb_candidates,
        kb_weights=kb_weights,
        kb_orders=kb_orders,
        rrf_k=rrf_k,
    )

    combined: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for c in high_candidates:
        cid = str(c.get("id") or "")
        if not cid or cid in seen_ids:
            continue
        seen_ids.add(cid)
        combined.append(c)
        if len(combined) >= int(top_k):
            break

    used_normal = False
    normal_obs: dict[str, Any] | None = None
    if len(combined) < int(top_k):
        used_normal = True
        normal_candidates, normal_obs = _merge_kb_candidates_rrf(
            kb_ids=normal_kb_ids,
            per_kb_candidates=per_kb_candidates,
            kb_weights=kb_weights,
            kb_orders=kb_orders,
            rrf_k=rrf_k,
        )
        for c in normal_candidates:
            cid = str(c.get("id") or "")
            if not cid or cid in seen_ids:
                continue
            seen_ids.add(cid)
            combined.append(c)
            if len(combined) >= int(top_k):
                break

    obs = {
        "mode": "priority",
        "priority_enabled": True,
        "rrf_k": int(rrf_k),
        "top_k": int(top_k),
        "groups": {"high": list(high_kb_ids), "normal": list(normal_kb_ids)},
        "high": high_obs,
        "normal": normal_obs,
        "used_normal": bool(used_normal),
        "candidate_count": int(len(combined)),
    }
    return combined, obs


def _parse_vector_source_order() -> list[str] | None:
    raw = str(getattr(settings, "vector_source_order", "") or "").strip()
    if not raw:
        return None
    parts = [p.strip().lower() for p in re.split(r"[\s,|;]+", raw) if p.strip()]
    out: list[str] = []
    for p in parts:
        if p not in _ALL_SOURCES:
            continue
        if p in out:
            continue
        out.append(p)
    return out or None


def _parse_vector_source_weights() -> dict[str, float] | None:
    raw = str(getattr(settings, "vector_source_weights_json", "") or "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except Exception:
        return None
    if not isinstance(value, dict):
        return None
    out: dict[str, float] = {}
    for k, v in value.items():
        source = str(k or "").strip().lower()
        if source not in _ALL_SOURCES:
            continue
        try:
            weight = float(v)
        except Exception:
            continue
        if weight <= 0:
            continue
        out[source] = weight
    return out or None


def _super_sort_final_chunks(
    final_chunks: list[dict[str, Any]], *, super_sort: dict[str, Any] | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    before_ids = [str(c.get("id") or "") for c in final_chunks if isinstance(c, dict)]

    requested = super_sort if isinstance(super_sort, dict) else None

    override_enabled: bool | None = None
    override_order: list[str] | None = None
    override_weights: dict[str, float] | None = None

    if requested is not None:
        if "enabled" in requested:
            override_enabled = bool(requested.get("enabled"))

        raw_order = requested.get("source_order")
        if isinstance(raw_order, str):
            parts = [p.strip().lower() for p in re.split(r"[\s,|;]+", raw_order) if p.strip()]
        elif isinstance(raw_order, list):
            parts = [str(p or "").strip().lower() for p in raw_order if str(p or "").strip()]
        else:
            parts = []
        if parts:
            order: list[str] = []
            for p in parts:
                if p not in _ALL_SOURCES:
                    continue
                if p in order:
                    continue
                order.append(p)
            override_order = order or None

        raw_weights = requested.get("source_weights")
        if isinstance(raw_weights, dict):
            out: dict[str, float] = {}
            for k, v in raw_weights.items():
                source = str(k or "").strip().lower()
                if source not in _ALL_SOURCES:
                    continue
                try:
                    weight = float(v)
                except Exception:
                    continue
                if weight <= 0:
                    continue
                out[source] = weight
            override_weights = out or None

    order_cfg = override_order if override_order is not None else _parse_vector_source_order()
    weights_cfg = override_weights if override_weights is not None else _parse_vector_source_weights()
    enabled = bool(order_cfg or weights_cfg)
    if override_enabled is False:
        enabled = False

    base_obs: dict[str, Any] = {
        "enabled": bool(enabled),
        "applied": False,
        "reason": "disabled" if not enabled else None,
        "override_enabled": override_enabled,
        "requested": requested,
        "source_order": order_cfg,
        "source_weights": weights_cfg,
        "before": before_ids,
        "after": list(before_ids),
    }
    if not enabled or len(final_chunks) <= 1:
        if enabled:
            base_obs["reason"] = "noop"
        return list(final_chunks), base_obs

    if order_cfg:
        order = list(order_cfg)
        for s in _ALL_SOURCES:
            if s not in order:
                order.append(s)
    else:
        weights_for_sort = weights_cfg or {}
        order = sorted(_ALL_SOURCES, key=lambda s: (-float(weights_for_sort.get(s, 1.0)), s))

    weights_for_all = {s: float((weights_cfg or {}).get(s, 1.0)) for s in _ALL_SOURCES}
    order_index = {s: i for i, s in enumerate(order)}

    grouped: dict[str, list[dict[str, Any]]] = {}
    for c in final_chunks:
        if not isinstance(c, dict):
            continue
        meta = c.get("metadata") if isinstance(c.get("metadata"), dict) else {}
        source = str(meta.get("source") or "")
        grouped.setdefault(source, []).append(c)

    def _natural_key(source: str, c: dict[str, Any]) -> tuple:
        meta = c.get("metadata") if isinstance(c.get("metadata"), dict) else {}
        try:
            chunk_index = int(meta.get("chunk_index") or 0)
        except Exception:
            chunk_index = 0
        source_id = str(meta.get("source_id") or "")
        cid = str(c.get("id") or "")
        if source == "chapter":
            try:
                chapter_number = int(meta.get("chapter_number") or 0)
            except Exception:
                chapter_number = 0
            return (chapter_number, chunk_index, source_id, cid)
        title = str(meta.get("title") or "")
        return (title, chunk_index, source_id, cid)

    for source, items in grouped.items():
        items.sort(key=lambda c: _natural_key(source, c))

    pos = {s: 0 for s in grouped.keys()}
    taken = {s: 0 for s in grouped.keys()}

    out: list[dict[str, Any]] = []
    while True:
        available = [s for s in grouped.keys() if pos.get(s, 0) < len(grouped[s])]
        if not available:
            break

        def _pick_key(s: str) -> tuple[float, int, str]:
            weight = float(weights_for_all.get(s, 1.0))
            if weight <= 0:
                weight = 1.0
            ratio = float(taken.get(s, 0)) / weight
            return (ratio, int(order_index.get(s, 999)), s)

        selected_source = min(available, key=_pick_key)
        out.append(grouped[selected_source][pos[selected_source]])
        pos[selected_source] = int(pos.get(selected_source, 0)) + 1
        taken[selected_source] = int(taken.get(selected_source, 0)) + 1

    after_ids = [str(c.get("id") or "") for c in out if isinstance(c, dict)]
    obs = dict(base_obs)
    obs.update(
        {
            "applied": after_ids != before_ids,
            "reason": "ok",
            "source_order_effective": order,
            "source_weights_effective": weights_for_all,
            "after": after_ids,
            "by_source": {s: len(grouped[s]) for s in sorted(grouped.keys())},
        }
    )
    return out, obs


def _build_vector_query_counts(
    *,
    candidates_total: int,
    returned_candidates: list[dict[str, Any]],
    final_selected: int,
    dropped: list[dict[str, Any]],
) -> dict[str, Any]:
    unique_keys: set[tuple[str, str]] = set()
    for c in returned_candidates:
        if isinstance(c, dict):
            unique_keys.add(_vector_candidate_key(c))

    dropped_by_reason: dict[str, int] = {}
    for d in dropped:
        if not isinstance(d, dict):
            continue
        reason = str(d.get("reason") or "")
        if not reason:
            continue
        dropped_by_reason[reason] = dropped_by_reason.get(reason, 0) + 1

    return {
        "candidates_total": int(candidates_total),
        "candidates_returned": int(len(returned_candidates)),
        "unique_sources": int(len(unique_keys)),
        "final_selected": int(final_selected),
        "dropped_total": int(len(dropped)),
        "dropped_by_reason": dropped_by_reason,
    }


def _vector_budget_observability(
    *,
    top_k: int,
    max_chunks: int,
    per_source_max_chunks: int,
    char_limit: int,
    dropped: list[dict[str, Any]],
) -> dict[str, Any]:
    return build_budget_observability(
        module="vector",
        limits={
            "max_candidates": int(top_k),
            "final_max_chunks": int(max_chunks),
            "per_source_max_chunks": int(per_source_max_chunks),
            "final_char_limit": int(char_limit),
        },
        dropped=dropped,
        reason_explain=_VECTOR_DROPPED_REASON_EXPLAIN,
    )

def vector_rag_status(
    *,
    project_id: str,
    sources: list[VectorSource] | None = None,
    embedding: dict[str, str | None] | None = None,
    rerank: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sources = sources or list(_ALL_SOURCES)
    enabled, disabled_reason = _vector_enabled_reason(embedding=embedding)
    rerank_enabled, rerank_method, rerank_top_k, rerank_hybrid_alpha = _resolve_rerank_config(rerank)
    rerank_external = _resolve_rerank_external_config(rerank)
    rerank_provider: str | None = None
    rerank_model: str | None = None
    rerank_method_effective: str | None = None
    if rerank_enabled:
        rerank_method_effective = str(rerank_method or "").strip() or None
        if rerank_method_effective == "external_rerank_api":
            rerank_provider = "external_rerank_api"
            rerank_model_raw = (rerank_external or {}).get("model")
            rerank_model = str(rerank_model_raw or "").strip() or None
        else:
            rerank_provider = "local"
            rerank_model = None
    rerank_obs = {
        "enabled": bool(rerank_enabled),
        "applied": False,
        "requested_method": rerank_method,
        "method": rerank_method_effective,
        "provider": rerank_provider,
        "model": rerank_model,
        "top_k": int(rerank_top_k),
        "hybrid_alpha": float(rerank_hybrid_alpha),
        "hybrid_applied": False,
        "after_rerank": [],
        "reason": "disabled" if not rerank_enabled else "status_only",
        "error_type": None,
        "before": [],
        "after": [],
        "timing_ms": 0,
        "errors": [],
    }
    if not enabled:
        return {
            "enabled": False,
            "disabled_reason": disabled_reason,
            "query_text": "",
            "filters": {"project_id": project_id, "sources": sources},
            "timings_ms": {"rerank": 0},
            "candidates": [],
            "final": {"chunks": [], "text_md": "", "truncated": False},
            "dropped": [],
            "counts": _build_vector_query_counts(candidates_total=0, returned_candidates=[], final_selected=0, dropped=[]),
            "prompt_block": {"identifier": "sys.memory.vector_rag", "role": "system", "text_md": ""},
            "backend_preferred": "pgvector" if _prefer_pgvector() else "chroma",
            "hybrid_enabled": bool(getattr(settings, "vector_hybrid_enabled", True)),
            "rerank": rerank_obs,
        }
    return {
        "enabled": True,
        "disabled_reason": None,
        "query_text": "",
        "filters": {"project_id": project_id, "sources": sources},
        "timings_ms": {"rerank": 0},
        "candidates": [],
        "final": {"chunks": [], "text_md": "", "truncated": False},
        "dropped": [],
        "counts": _build_vector_query_counts(candidates_total=0, returned_candidates=[], final_selected=0, dropped=[]),
        "prompt_block": {"identifier": "sys.memory.vector_rag", "role": "system", "text_md": ""},
        "backend_preferred": "pgvector" if _prefer_pgvector() else "chroma",
        "hybrid_enabled": bool(getattr(settings, "vector_hybrid_enabled", True)),
        "rerank": rerank_obs,
    }

def _format_final_text(chunks: list[dict[str, Any]], *, char_limit: int) -> tuple[str, bool]:
    parts: list[str] = []
    for c in chunks:
        meta = c.get("metadata") if isinstance(c.get("metadata"), dict) else {}
        source = str(meta.get("source") or "")
        title = str(meta.get("title") or "").strip()
        if source == "chapter":
            n = meta.get("chapter_number")
            header = f"【章节 {n}：{title or meta.get('source_id') or 'chapter'}】"
        elif source == "outline":
            header = f"【大纲：{title or meta.get('source_id') or 'outline'}】"
        elif source == "story_memory":
            mtype = str(meta.get("memory_type") or "").strip() or "story_memory"
            header = f"【记忆：{mtype}：{title or meta.get('source_id') or 'memory'}】".rstrip("：")
        else:
            header = f"【{source or 'chunk'}】"
        text = str(c.get("text") or "").strip()
        if not text:
            continue
        parts.append(f"{header}\n{text}".strip())

    inner = "\n\n---\n\n".join(parts).strip()
    truncated = False
    if char_limit >= 0 and inner and len(inner) > char_limit:
        inner = inner[:char_limit].rstrip()
        truncated = True
    if not inner:
        return "", False
    return f"<VECTOR_RAG>\n{inner}\n</VECTOR_RAG>", truncated


def query_project(
    *,
    project_id: str,
    kb_id: str | None = None,
    kb_ids: list[str] | None = None,
    query_text: str,
    sources: list[VectorSource] | None = None,
    embedding: dict[str, str | None] | None = None,
    rerank: dict[str, Any] | None = None,
    super_sort: dict[str, Any] | None = None,
    kb_weights: dict[str, float] | None = None,
    kb_orders: dict[str, int] | None = None,
    kb_priority_groups: dict[str, str] | None = None,
) -> dict[str, Any]:
    raw_kb_ids = kb_ids if kb_ids is not None else ([kb_id] if kb_id is not None else [])
    selected_kb_ids: list[str] = []
    seen_kb: set[str] = set()
    for raw in raw_kb_ids:
        normalized = _normalize_kb_id(str(raw or "").strip() or None)
        if normalized in seen_kb:
            continue
        seen_kb.add(normalized)
        selected_kb_ids.append(normalized)
    if not selected_kb_ids:
        selected_kb_ids = [_normalize_kb_id(None)]

    weights_by_kb_full = {kb: float((kb_weights or {}).get(kb, 1.0)) for kb in selected_kb_ids}
    orders_by_kb_full = {kb: int((kb_orders or {}).get(kb, 999)) for kb in selected_kb_ids}
    priority_groups_by_kb_full = {kb: _normalize_kb_priority_group((kb_priority_groups or {}).get(kb)) for kb in selected_kb_ids}

    if _prefer_pgvector() and len(selected_kb_ids) > 1:
        selected_kb_ids = [
            sorted(
                selected_kb_ids,
                key=lambda kb: (
                    0 if priority_groups_by_kb_full.get(kb) == "high" else 1,
                    int(orders_by_kb_full.get(kb, 999)),
                    str(kb),
                ),
            )[0]
        ]

    weights_by_kb = {kb: float(weights_by_kb_full.get(kb, 1.0)) for kb in selected_kb_ids}
    orders_by_kb = {kb: int(orders_by_kb_full.get(kb, 999)) for kb in selected_kb_ids}
    priority_groups_by_kb = {kb: str(priority_groups_by_kb_full.get(kb, "normal") or "normal") for kb in selected_kb_ids}

    sources = sources or list(_ALL_SOURCES)
    enabled, disabled_reason = _vector_enabled_reason(embedding=embedding)
    rerank_enabled, rerank_method, rerank_top_k, rerank_hybrid_alpha = _resolve_rerank_config(rerank)
    rerank_external = _resolve_rerank_external_config(rerank)
    rerank_provider: str | None = None
    rerank_model: str | None = None
    rerank_method_effective: str | None = None
    if rerank_enabled:
        rerank_method_effective = str(rerank_method or "").strip() or None
        if rerank_method_effective == "external_rerank_api":
            rerank_provider = "external_rerank_api"
            rerank_model_raw = (rerank_external or {}).get("model")
            rerank_model = str(rerank_model_raw or "").strip() or None
        else:
            rerank_provider = "local"
            rerank_model = None
    if not enabled:
        rerank_obs = {
            "enabled": bool(rerank_enabled),
            "applied": False,
            "requested_method": rerank_method,
            "method": rerank_method_effective,
            "provider": rerank_provider,
            "model": rerank_model,
            "top_k": int(rerank_top_k),
            "hybrid_alpha": float(rerank_hybrid_alpha),
            "hybrid_applied": False,
            "after_rerank": [],
            "reason": "vector_disabled",
            "error_type": None,
            "before": [],
            "after": [],
            "timing_ms": 0,
            "errors": [],
        }
        return {
            "enabled": False,
            "disabled_reason": disabled_reason,
            "query_text": query_text,
            "filters": {"project_id": project_id, "sources": sources},
            "timings_ms": {"rerank": 0},
            "candidates": [],
            "final": {"chunks": [], "text_md": "", "truncated": False},
            "dropped": [],
            "counts": _build_vector_query_counts(candidates_total=0, returned_candidates=[], final_selected=0, dropped=[]),
            "prompt_block": {"identifier": "sys.memory.vector_rag", "role": "system", "text_md": ""},
            "rerank": rerank_obs,
            "kbs": {
                "selected": selected_kb_ids,
                "weights": weights_by_kb,
                "orders": orders_by_kb,
                "priority_groups": priority_groups_by_kb,
                "merge": {"mode": "none", "reason": "vector_disabled"},
                "per_kb": {},
            },
        }

    start = time.perf_counter()
    embed_out = embed_texts_with_providers([query_text.strip() or " "], embedding=embedding)
    embed_ms = int((time.perf_counter() - start) * 1000)
    if not bool(embed_out.get("enabled")):
        disabled = str(embed_out.get("disabled_reason") or "error")
        error = embed_out.get("error")
        rerank_obs = {
            "enabled": bool(rerank_enabled),
            "applied": False,
            "requested_method": rerank_method,
            "method": None,
            "provider": None,
            "model": None,
            "top_k": int(rerank_top_k),
            "hybrid_alpha": float(rerank_hybrid_alpha),
            "hybrid_applied": False,
            "after_rerank": [],
            "reason": "vector_error" if disabled == "error" else "vector_disabled",
            "error_type": "EmbeddingError" if error else None,
            "before": [],
            "after": [],
            "timing_ms": 0,
            "errors": [],
        }
        return {
            "enabled": False,
            "disabled_reason": disabled,
            "error": error,
            "error_type": "EmbeddingError" if error else None,
            "query_text": query_text,
            "filters": {"project_id": project_id, "sources": sources},
            "timings_ms": {"embed": embed_ms, "rerank": 0},
            "candidates": [],
            "final": {"chunks": [], "text_md": "", "truncated": False},
            "dropped": [],
            "counts": _build_vector_query_counts(candidates_total=0, returned_candidates=[], final_selected=0, dropped=[]),
            "prompt_block": {"identifier": "sys.memory.vector_rag", "role": "system", "text_md": ""},
            "rerank": rerank_obs,
            "kbs": {
                "selected": selected_kb_ids,
                "weights": weights_by_kb,
                "orders": orders_by_kb,
                "priority_groups": priority_groups_by_kb,
                "merge": {"mode": "none", "reason": str(disabled)},
                "per_kb": {},
            },
        }

    qvec = (embed_out.get("vectors") or [[]])[0]

    top_k = int(settings.vector_max_candidates or 20)
    pgvector_error: str | None = None
    if _prefer_pgvector() and bool(getattr(settings, "vector_hybrid_enabled", True)):
        query_start = time.perf_counter()
        try:
            hybrid_out = _pgvector_hybrid_query(project_id=project_id, query_text=query_text, query_vec=qvec, sources=sources)
            query_ms = int((time.perf_counter() - query_start) * 1000)

            raw_candidates = hybrid_out.get("candidates") if isinstance(hybrid_out.get("candidates"), list) else []
            candidates: list[dict[str, Any]] = []
            for c in raw_candidates:
                if not isinstance(c, dict):
                    continue
                cc = dict(c)
                cc.pop("_rrf_score", None)
                candidates.append(cc)

            trimmed_candidates = candidates[:top_k]
            if not rerank_enabled:
                before_ids = [str(c.get("id") or "") for c in trimmed_candidates if isinstance(c, dict)]
                rerank_obs: dict[str, Any] = {
                    "enabled": False,
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
                    "before": before_ids,
                    "after": list(before_ids),
                    "timing_ms": 0,
                    "errors": [],
                }
            elif trimmed_candidates:
                trimmed_candidates, rerank_obs = _rerank_candidates(
                    query_text=query_text,
                    candidates=trimmed_candidates,
                    method=rerank_method,
                    top_k=rerank_top_k,
                    hybrid_alpha=rerank_hybrid_alpha,
                    external=rerank_external,
                )
            else:
                rerank_obs = {
                    "enabled": True,
                    "applied": False,
                    "requested_method": rerank_method,
                    "method": None,
                    "provider": None,
                    "model": None,
                    "top_k": int(rerank_top_k),
                    "hybrid_alpha": float(rerank_hybrid_alpha),
                    "hybrid_applied": False,
                    "after_rerank": [],
                    "reason": "empty_candidates",
                    "error_type": None,
                    "before": [],
                    "after": [],
                    "timing_ms": 0,
                    "errors": [],
                }
            dropped: list[dict[str, Any]] = []
            final_chunks: list[dict[str, Any]] = []
            seen_chunk_keys: set[tuple[str, str, int]] = set()
            selected_by_source: dict[tuple[str, str], int] = {}
            max_chunks = int(settings.vector_final_max_chunks or 6)
            max_chunks = max(1, min(int(max_chunks), 1000))
            per_source_max_chunks = int(getattr(settings, "vector_per_source_id_max_chunks", 1) or 1)
            per_source_max_chunks = max(1, min(int(per_source_max_chunks), 1000))
            processed = 0
            for c in trimmed_candidates:
                processed += 1
                source_key = _vector_candidate_key(c)
                chunk_key = _vector_candidate_chunk_key(c)
                if chunk_key in seen_chunk_keys:
                    dropped.append({"id": c.get("id"), "reason": "duplicate_chunk"})
                    continue
                seen_chunk_keys.add(chunk_key)
                if selected_by_source.get(source_key, 0) >= per_source_max_chunks:
                    dropped.append({"id": c.get("id"), "reason": "per_source_budget"})
                    continue
                final_chunks.append(c)
                selected_by_source[source_key] = selected_by_source.get(source_key, 0) + 1
                if len(final_chunks) >= max_chunks:
                    break

            if len(final_chunks) >= max_chunks:
                for c in trimmed_candidates[processed:]:
                    dropped.append({"id": c.get("id"), "reason": "budget"})

            final_chunks, super_sort_obs = _super_sort_final_chunks(final_chunks, super_sort=super_sort)

            final_char_limit = int(settings.vector_final_char_limit or 6000)
            post_start = time.perf_counter()
            text_md, truncated = _format_final_text(final_chunks, char_limit=final_char_limit)
            post_ms = int((time.perf_counter() - post_start) * 1000)

            timings_ms = {"embed": embed_ms, "query": query_ms, "post": post_ms, "rerank": int(rerank_obs.get("timing_ms") or 0)}
            obs_counts = _build_vector_query_counts(
                candidates_total=len(candidates),
                returned_candidates=trimmed_candidates,
                final_selected=len(final_chunks),
                dropped=dropped,
            )
            budget_obs = _vector_budget_observability(
                top_k=top_k,
                max_chunks=max_chunks,
                per_source_max_chunks=per_source_max_chunks,
                char_limit=final_char_limit,
                dropped=dropped,
            )
            log_event(
                logger,
                "info",
                event="VECTOR_RAG",
                action="query",
                project_id=project_id,
                backend="pgvector",
                hybrid_enabled=True,
                query_chars=len(query_text or ""),
                candidates=[c.get("id") for c in trimmed_candidates[: min(5, len(trimmed_candidates))]],
                dropped=dropped[:5],
                timings_ms=timings_ms,
                filters={"sources": sources},
                overfilter=hybrid_out.get("overfilter"),
                counts=hybrid_out.get("counts"),
                rerank=rerank_obs,
                super_sort=super_sort_obs,
            )

            return {
                "enabled": True,
                "disabled_reason": None,
                "query_text": query_text,
                "filters": {"project_id": project_id, "sources": sources},
                "timings_ms": timings_ms,
                "candidates": trimmed_candidates,
                "final": {"chunks": final_chunks, "text_md": text_md, "truncated": truncated},
                "dropped": dropped,
                "counts": obs_counts,
                "budget_observability": budget_obs,
                "rerank": rerank_obs,
                "super_sort": super_sort_obs,
                "prompt_block": {"identifier": "sys.memory.vector_rag", "role": "system", "text_md": text_md},
                "backend": "pgvector",
                "hybrid": {
                    "enabled": True,
                    "ranks": hybrid_out.get("ranks"),
                    "counts": hybrid_out.get("counts"),
                    "overfilter": hybrid_out.get("overfilter"),
                },
            }
        except Exception as exc:  # pragma: no cover - env dependent
            pgvector_error = type(exc).__name__

    per_kb: dict[str, Any] = {}
    per_kb_candidates: dict[str, list[dict[str, Any]]] = {}
    query_ms = 0

    for kid in selected_kb_ids:
        try:
            collection = _get_collection(project_id=project_id, kb_id=kid)
        except Exception as exc:  # pragma: no cover - env dependent
            per_kb[kid] = {
                "enabled": False,
                "disabled_reason": "chroma_unavailable",
                "error": str(exc),
                "counts": _build_vector_query_counts(candidates_total=0, returned_candidates=[], final_selected=0, dropped=[]),
                "overfilter": None,
                "weight": float(weights_by_kb.get(kid, 1.0)),
                "order": int(orders_by_kb.get(kid, 999)),
                "priority_group": str(priority_groups_by_kb.get(kid, "normal") or "normal"),
            }
            continue

        query_start = time.perf_counter()
        where: dict[str, Any] | None = None
        if len(sources) == 1:
            where = {"source": sources[0]}
        result = collection.query(
            query_embeddings=[qvec],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        query_ms += int((time.perf_counter() - query_start) * 1000)

        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]

        candidates: list[dict[str, Any]] = []
        for idx in range(min(len(ids), len(docs), len(metas), len(dists))):
            meta = metas[idx] if isinstance(metas[idx], dict) else {}
            if sources and str(meta.get("source") or "") not in sources:
                continue
            m = dict(meta)
            m.setdefault("kb_id", kid)
            candidates.append(
                {
                    "id": str(ids[idx]),
                    "distance": float(dists[idx]),
                    "text": str(docs[idx] or ""),
                    "metadata": m,
                }
            )

        per_kb_candidates[kid] = candidates
        per_kb[kid] = {
            "enabled": True,
            "disabled_reason": None,
            "counts": _build_vector_query_counts(candidates_total=len(candidates), returned_candidates=candidates[:top_k], final_selected=0, dropped=[]),
            "overfilter": None,
            "weight": float(weights_by_kb.get(kid, 1.0)),
            "order": int(orders_by_kb.get(kid, 999)),
            "priority_group": str(priority_groups_by_kb.get(kid, "normal") or "normal"),
        }

    if not per_kb_candidates:
        out: dict[str, Any] = {
            "enabled": False,
            "disabled_reason": "chroma_unavailable",
            "error": "no_collections",
            "query_text": query_text,
            "filters": {"project_id": project_id, "sources": sources},
            "timings_ms": {"embed": embed_ms, "rerank": 0},
            "candidates": [],
            "final": {"chunks": [], "text_md": "", "truncated": False},
            "dropped": [],
            "counts": _build_vector_query_counts(candidates_total=0, returned_candidates=[], final_selected=0, dropped=[]),
            "prompt_block": {"identifier": "sys.memory.vector_rag", "role": "system", "text_md": ""},
            "rerank": {
                "enabled": bool(rerank_enabled),
                "applied": False,
                "requested_method": rerank_method,
                "method": None,
                "provider": None,
                "model": None,
                "top_k": int(rerank_top_k),
                "hybrid_alpha": float(rerank_hybrid_alpha),
                "hybrid_applied": False,
                "after_rerank": [],
                "reason": "chroma_unavailable",
                "error_type": None,
                "before": [],
                "after": [],
                "timing_ms": 0,
                "errors": [],
            },
            "kbs": {
                "selected": selected_kb_ids,
                "weights": weights_by_kb,
                "orders": orders_by_kb,
                "priority_groups": priority_groups_by_kb,
                "merge": {"mode": "none", "reason": "no_collections"},
                "per_kb": per_kb,
            },
        }
        if pgvector_error:
            out["fallback"] = {"from": "pgvector", "to": "chroma", "error": pgvector_error}
        return out

    rrf_k = int(settings.vector_hybrid_rrf_k or 60)
    priority_enabled = bool(getattr(settings, "vector_priority_retrieval_enabled", False))
    candidates, merge_obs = _merge_kb_candidates(
        kb_ids=selected_kb_ids,
        per_kb_candidates=per_kb_candidates,
        kb_weights=weights_by_kb,
        kb_orders=orders_by_kb,
        kb_priority_groups=priority_groups_by_kb,
        top_k=top_k,
        priority_enabled=priority_enabled,
        rrf_k=rrf_k,
    )

    trimmed_candidates = candidates[:top_k]
    if not rerank_enabled:
        before_ids = [str(c.get("id") or "") for c in trimmed_candidates if isinstance(c, dict)]
        rerank_obs = {
            "enabled": False,
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
            "before": before_ids,
            "after": list(before_ids),
            "timing_ms": 0,
            "errors": [],
        }
    elif trimmed_candidates:
        trimmed_candidates, rerank_obs = _rerank_candidates(
            query_text=query_text,
            candidates=trimmed_candidates,
            method=rerank_method,
            top_k=rerank_top_k,
            hybrid_alpha=rerank_hybrid_alpha,
            external=rerank_external,
        )
    else:
        rerank_obs = {
            "enabled": True,
            "applied": False,
            "requested_method": rerank_method,
            "method": None,
            "provider": None,
            "model": None,
            "top_k": int(rerank_top_k),
            "hybrid_alpha": float(rerank_hybrid_alpha),
            "hybrid_applied": False,
            "after_rerank": [],
            "reason": "empty_candidates",
            "error_type": None,
            "before": [],
            "after": [],
            "timing_ms": 0,
            "errors": [],
        }

    dropped: list[dict[str, Any]] = []
    final_chunks: list[dict[str, Any]] = []
    seen_chunk_keys: set[tuple[str, str, int]] = set()
    selected_by_source: dict[tuple[str, str], int] = {}
    max_chunks = int(settings.vector_final_max_chunks or 6)
    max_chunks = max(1, min(int(max_chunks), 1000))
    per_source_max_chunks = int(getattr(settings, "vector_per_source_id_max_chunks", 1) or 1)
    per_source_max_chunks = max(1, min(int(per_source_max_chunks), 1000))
    processed = 0
    for c in trimmed_candidates:
        processed += 1
        source_key = _vector_candidate_key(c)
        chunk_key = _vector_candidate_chunk_key(c)
        if chunk_key in seen_chunk_keys:
            dropped.append({"id": c.get("id"), "reason": "duplicate_chunk"})
            continue
        seen_chunk_keys.add(chunk_key)
        if selected_by_source.get(source_key, 0) >= per_source_max_chunks:
            dropped.append({"id": c.get("id"), "reason": "per_source_budget"})
            continue
        final_chunks.append(c)
        selected_by_source[source_key] = selected_by_source.get(source_key, 0) + 1
        if len(final_chunks) >= max_chunks:
            break

    if len(final_chunks) >= max_chunks:
        for c in trimmed_candidates[processed:]:
            dropped.append({"id": c.get("id"), "reason": "budget"})

    final_chunks, super_sort_obs = _super_sort_final_chunks(final_chunks, super_sort=super_sort)

    final_char_limit = int(settings.vector_final_char_limit or 6000)
    post_start = time.perf_counter()
    text_md, truncated = _format_final_text(final_chunks, char_limit=final_char_limit)
    post_ms = int((time.perf_counter() - post_start) * 1000)

    timings_ms = {"embed": embed_ms, "query": query_ms, "post": post_ms, "rerank": int(rerank_obs.get("timing_ms") or 0)}
    obs_counts = _build_vector_query_counts(
        candidates_total=len(candidates),
        returned_candidates=trimmed_candidates,
        final_selected=len(final_chunks),
        dropped=dropped,
    )
    budget_obs = _vector_budget_observability(
        top_k=top_k,
        max_chunks=max_chunks,
        per_source_max_chunks=per_source_max_chunks,
        char_limit=final_char_limit,
        dropped=dropped,
    )
    log_event(
        logger,
        "info",
        event="VECTOR_RAG",
        action="query",
        project_id=project_id,
        backend="chroma",
        query_chars=len(query_text or ""),
        candidates=[c.get("id") for c in trimmed_candidates[: min(5, len(trimmed_candidates))]],
        dropped=dropped[:5],
        timings_ms=timings_ms,
        filters={"sources": sources},
        rerank=rerank_obs,
        super_sort=super_sort_obs,
    )

    out: dict[str, Any] = {
        "enabled": True,
        "disabled_reason": None,
        "query_text": query_text,
        "filters": {"project_id": project_id, "sources": sources},
        "timings_ms": timings_ms,
        "candidates": trimmed_candidates,
        "final": {"chunks": final_chunks, "text_md": text_md, "truncated": truncated},
        "dropped": dropped,
        "counts": obs_counts,
        "budget_observability": budget_obs,
        "rerank": rerank_obs,
        "super_sort": super_sort_obs,
        "prompt_block": {"identifier": "sys.memory.vector_rag", "role": "system", "text_md": text_md},
        "backend": "chroma",
        "kbs": {
            "selected": selected_kb_ids,
            "weights": weights_by_kb,
            "orders": orders_by_kb,
            "priority_groups": priority_groups_by_kb,
            "merge": merge_obs,
            "per_kb": per_kb,
        },
    }
    if pgvector_error:
        out["fallback"] = {"from": "pgvector", "to": "chroma", "error": pgvector_error}
    return out
