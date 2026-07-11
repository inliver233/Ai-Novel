from __future__ import annotations

import hashlib
import json
from typing import Any

from app.core.secrets import SecretCryptoError, decrypt_secret
from app.models.project_settings import ProjectSettings
from app.services.embedding_service import resolve_embedding_config


def _base_embedding_overrides(row: ProjectSettings | None) -> dict[str, Any]:
    """
    Resolve per-project embedding overrides from ProjectSettings.

    Note: this is runtime-only config (may include a decrypted `api_key`).
    Never serialize this dict without passing through `redact_api_keys(...)`.
    """

    if row is None:
        return {}

    out: dict[str, Any] = {}

    provider = str(getattr(row, "vector_embedding_provider", "") or "").strip()
    if provider:
        out["provider"] = provider

    base_url = str(row.vector_embedding_base_url or "").strip()
    if base_url:
        out["base_url"] = base_url

    model = str(row.vector_embedding_model or "").strip()
    if model:
        out["model"] = model

    azure_deployment = str(getattr(row, "vector_embedding_azure_deployment", "") or "").strip()
    if azure_deployment:
        out["azure_deployment"] = azure_deployment

    azure_api_version = str(getattr(row, "vector_embedding_azure_api_version", "") or "").strip()
    if azure_api_version:
        out["azure_api_version"] = azure_api_version

    st_model = str(getattr(row, "vector_embedding_sentence_transformers_model", "") or "").strip()
    if st_model:
        out["sentence_transformers_model"] = st_model

    out["expected_dimension"] = int(getattr(row, "vector_embedding_expected_dimension", 1536) or 1536)

    if row.vector_embedding_api_key_ciphertext:
        try:
            api_key = decrypt_secret(row.vector_embedding_api_key_ciphertext).strip()
        except SecretCryptoError:
            api_key = ""
        if api_key:
            out["api_key"] = api_key

    return out


def embedding_config_fingerprint(row: ProjectSettings | None) -> str:
    config = resolve_embedding_config(_base_embedding_overrides(row))
    api_key_hash = hashlib.sha256(str(config.api_key or "").encode("utf-8")).hexdigest()
    identity = {
        "provider": config.provider,
        "base_url": str(config.base_url or "").rstrip("/"),
        "model": config.model,
        "azure_deployment": config.azure_deployment,
        "azure_api_version": config.azure_api_version,
        "sentence_transformers_model": config.sentence_transformers_model,
        "expected_dimension": int(config.expected_dimension),
        "api_key_sha256": api_key_hash,
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def vector_embedding_overrides(row: ProjectSettings | None) -> dict[str, Any]:
    return _base_embedding_overrides(row)
