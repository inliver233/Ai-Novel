from __future__ import annotations

import re
from typing import Any

from app.core.config import settings
from app.services.rerank_service import rerank_candidates as rerank_candidates_with_providers

_RERANK_TOKEN_RE = re.compile("[A-Za-z0-9\u4e00-\u9fff]+")


def _rerank_tokens(text: str) -> set[str]:
    if not text:
        return set()
    return {t.lower() for t in _RERANK_TOKEN_RE.findall(text) if t.strip()}


def _rerank_score(*, method: str, query_text: str, candidate_text: str) -> float:
    qtext = (query_text or "").strip()
    if not qtext:
        return 0.0

    if method == "rapidfuzz_token_set_ratio":
        from rapidfuzz import fuzz  # type: ignore[import-not-found]

        return float(fuzz.token_set_ratio(qtext, candidate_text or "")) / 100.0

    q_tokens = _rerank_tokens(qtext)
    if not q_tokens:
        return 0.0
    c_tokens = _rerank_tokens(candidate_text or "")
    if not c_tokens:
        return 0.0
    return float(len(q_tokens & c_tokens)) / float(len(q_tokens))


def _rerank_candidates(
    *,
    query_text: str,
    candidates: list[dict[str, Any]],
    method: str,
    top_k: int,
    hybrid_alpha: float | None = None,
    external: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return rerank_candidates_with_providers(
        query_text=query_text,
        candidates=candidates,
        method=method,
        top_k=top_k,
        hybrid_alpha=hybrid_alpha,
        external=external,
        score_fn=_rerank_score,
    )


def _resolve_rerank_config(rerank: dict[str, Any] | None) -> tuple[bool, str, int, float]:
    enabled = bool(getattr(settings, "vector_rerank_enabled", False))
    method = "auto"
    top_k = int(getattr(settings, "vector_max_candidates", 20) or 20)
    hybrid_alpha = 0.0
    if rerank is None:
        return enabled, method, max(1, min(int(top_k), 1000)), float(hybrid_alpha)

    if "enabled" in rerank:
        enabled = bool(rerank.get("enabled"))
    raw_method = str(rerank.get("method") or "").strip()
    if raw_method:
        method = raw_method

    provider_raw = str(rerank.get("provider") or "").strip()
    if method == "auto" and provider_raw == "external_rerank_api":
        method = "external_rerank_api"
    if "top_k" in rerank and rerank.get("top_k") is not None:
        try:
            top_k = int(rerank.get("top_k"))
        except Exception:
            pass
    if "hybrid_alpha" in rerank and rerank.get("hybrid_alpha") is not None:
        try:
            hybrid_alpha = float(rerank.get("hybrid_alpha"))
        except Exception:
            hybrid_alpha = 0.0
    hybrid_alpha = max(0.0, min(float(hybrid_alpha), 1.0))
    return enabled, method, max(1, min(int(top_k), 1000)), float(hybrid_alpha)


def _resolve_rerank_external_config(rerank: dict[str, Any] | None) -> dict[str, Any] | None:
    if rerank is None:
        return None
    if not isinstance(rerank, dict):
        return None

    out: dict[str, Any] = {}

    base_url = str(rerank.get("base_url") or "").strip()
    if base_url:
        out["base_url"] = base_url

    model = str(rerank.get("model") or "").strip()
    if model:
        out["model"] = model

    api_key = str(rerank.get("api_key") or "").strip()
    if api_key:
        out["api_key"] = api_key

    timeout_seconds = rerank.get("timeout_seconds")
    if timeout_seconds is not None:
        out["timeout_seconds"] = timeout_seconds

    return out or None
