from __future__ import annotations

import importlib.util
import threading
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import settings
from app.core.errors import AppError
from app.llm.http_client import get_llm_http_client
from app.llm.utils import normalize_base_url

EmbeddingProvider = Literal[
    "openai_compatible",
    "azure_openai",
    "google",
    "custom",
    "local_proxy",
    "sentence_transformers",
]

_ST_MODEL_LOCK = threading.Lock()
_ST_MODELS: dict[tuple[str, str | None, str | None], object] = {}


def _sentence_transformers_available() -> bool:
    return importlib.util.find_spec("sentence_transformers") is not None


class EmbeddingConfig(BaseModel):
    provider: EmbeddingProvider = "openai_compatible"
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None

    azure_deployment: str | None = None
    azure_api_version: str | None = None

    sentence_transformers_model: str | None = None
    sentence_transformers_cache_dir: str | None = None
    sentence_transformers_device: str | None = None

    timeout_seconds: float = Field(default=60.0, ge=1.0, le=120.0)
    expected_dimension: int = Field(default=1536, ge=1, le=65535)

    model_config = ConfigDict(extra="allow")

    @field_validator("provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: object) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return "openai_compatible"
        raw = raw.replace("-", "_")
        aliases = {
            "openai": "openai_compatible",
            "openai_compat": "openai_compatible",
            "azure": "azure_openai",
            "azure_openai": "azure_openai",
            "google": "google",
            "gemini": "google",
            "custom": "custom",
            "local_proxy": "local_proxy",
            "sentence_transformers": "sentence_transformers",
            "sentence_transformer": "sentence_transformers",
            "st": "sentence_transformers",
        }
        return aliases.get(raw, raw)

    @field_validator(
        "base_url",
        "model",
        "api_key",
        "azure_deployment",
        "azure_api_version",
        "sentence_transformers_model",
        "sentence_transformers_cache_dir",
        "sentence_transformers_device",
        mode="before",
    )
    @classmethod
    def _strip_or_none(cls, value: object) -> str | None:
        raw = str(value or "").strip()
        return raw or None

    @model_validator(mode="after")
    def _compat_fill_fields(self) -> "EmbeddingConfig":
        if self.provider == "azure_openai" and not self.azure_deployment and self.model:
            self.azure_deployment = self.model
        if self.provider == "sentence_transformers" and not self.sentence_transformers_model and self.model:
            self.sentence_transformers_model = self.model
        return self


def resolve_embedding_config(embedding: dict[str, Any] | None = None) -> EmbeddingConfig:
    provider = str(
        (embedding or {}).get("provider") or getattr(settings, "vector_embedding_provider", "") or ""
    ).strip()
    provider = provider or "openai_compatible"

    base_url = (embedding or {}).get("base_url") or settings.vector_embedding_base_url
    model = (embedding or {}).get("model") or settings.vector_embedding_model
    api_key = (embedding or {}).get("api_key") or settings.vector_embedding_api_key

    azure_deployment = (embedding or {}).get("azure_deployment") or getattr(
        settings, "vector_embedding_azure_deployment", None
    )
    azure_api_version = (embedding or {}).get("azure_api_version") or getattr(
        settings, "vector_embedding_azure_api_version", None
    )

    st_model = (embedding or {}).get("sentence_transformers_model") or getattr(
        settings, "vector_embedding_sentence_transformers_model", None
    )
    st_cache_dir = (embedding or {}).get("sentence_transformers_cache_dir") or getattr(
        settings, "vector_embedding_sentence_transformers_cache_dir", None
    )
    st_device = (embedding or {}).get("sentence_transformers_device") or getattr(
        settings, "vector_embedding_sentence_transformers_device", None
    )

    timeout_seconds = (embedding or {}).get("timeout_seconds")
    expected_dimension = (embedding or {}).get("expected_dimension") or getattr(
        settings, "vector_embedding_expected_dimension", 1536
    )

    payload: dict[str, Any] = {
        **(embedding or {}),
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "azure_deployment": azure_deployment,
        "azure_api_version": azure_api_version,
        "sentence_transformers_model": st_model,
        "sentence_transformers_cache_dir": st_cache_dir,
        "sentence_transformers_device": st_device,
        "expected_dimension": expected_dimension,
    }
    if timeout_seconds is not None:
        payload["timeout_seconds"] = timeout_seconds

    return EmbeddingConfig.model_validate(payload)


def embedding_enabled_reason(config: EmbeddingConfig) -> tuple[bool, str | None]:
    if config.provider in ("openai_compatible", "custom", "local_proxy"):
        if not config.base_url:
            return False, "embedding_base_url_missing"
        if not config.model:
            return False, "embedding_model_missing"
        if not config.api_key:
            return False, "embedding_api_key_missing"
        return True, None

    if config.provider == "azure_openai":
        if not config.base_url:
            return False, "embedding_base_url_missing"
        if not config.azure_deployment:
            return False, "embedding_azure_deployment_missing"
        if not config.azure_api_version:
            return False, "embedding_azure_api_version_missing"
        if not config.api_key:
            return False, "embedding_api_key_missing"
        return True, None

    if config.provider == "google":
        if not config.base_url:
            return False, "embedding_base_url_missing"
        if not config.model:
            return False, "embedding_model_missing"
        if not config.api_key:
            return False, "embedding_api_key_missing"
        return True, None

    if config.provider == "sentence_transformers":
        if not config.sentence_transformers_model:
            return False, "embedding_sentence_transformers_model_missing"
        if not _sentence_transformers_available():
            return False, "dependency_missing"
        return True, None

    return False, "embedding_provider_unsupported"


def embed_texts(texts: list[str], *, embedding: dict[str, Any] | None = None) -> dict[str, Any]:
    config = resolve_embedding_config(embedding)
    if not texts:
        raise AppError(
            code="EMBEDDING_BATCH_EMPTY",
            message="Embedding 输入不能为空",
            status_code=422,
            details={"expected_dimension": config.expected_dimension},
        )
    enabled, disabled_reason = embedding_enabled_reason(config)
    if not enabled:
        return {
            "enabled": False,
            "disabled_reason": disabled_reason,
            "provider": config.provider,
            "vectors": [],
            "error": None,
        }

    try:
        if config.provider in ("openai_compatible", "custom", "local_proxy"):
            vectors = _embed_openai_compatible(texts, config=config)
        elif config.provider == "azure_openai":
            vectors = _embed_azure_openai(texts, config=config)
        elif config.provider == "google":
            vectors = _embed_google_gemini(texts, config=config)
        elif config.provider == "sentence_transformers":
            vectors = _embed_sentence_transformers(texts, config=config)
        else:
            return {
                "enabled": False,
                "disabled_reason": "embedding_provider_unsupported",
                "provider": config.provider,
                "vectors": [],
                "error": None,
            }
    except Exception as exc:
        return {
            "enabled": False,
            "disabled_reason": "error",
            "provider": config.provider,
            "vectors": [],
            "error": str(exc),
        }

    _validate_embedding_vectors(texts=texts, vectors=vectors, expected_dimension=config.expected_dimension)
    return {
        "enabled": True,
        "disabled_reason": None,
        "provider": config.provider,
        "vectors": vectors,
        "error": None,
        "dimension": config.expected_dimension,
    }


def _validate_embedding_vectors(*, texts: list[str], vectors: list[list[float]], expected_dimension: int) -> None:
    dimensions = [len(vector) for vector in vectors]
    if (
        len(vectors) != len(texts)
        or not vectors
        or any(dimension <= 0 for dimension in dimensions)
        or len(set(dimensions)) != 1
        or dimensions[0] != int(expected_dimension)
    ):
        raise AppError(
            code="EMBEDDING_DIMENSION_MISMATCH",
            message="Embedding 向量维度与配置不一致",
            status_code=422,
            details={
                "inputs": len(texts),
                "vectors": len(vectors),
                "dimensions": dimensions,
                "expected_dimension": int(expected_dimension),
            },
        )


def _embed_openai_compatible(texts: list[str], *, config: EmbeddingConfig) -> list[list[float]]:
    base_url = normalize_base_url(str(config.base_url or ""))
    model = str(config.model or "")
    api_key = str(config.api_key or "")

    url = base_url.rstrip("/") + "/embeddings"
    client = get_llm_http_client()
    resp = client.post(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "input": texts},
        timeout=float(config.timeout_seconds),
    )
    resp.raise_for_status()
    payload = resp.json()
    return _parse_openai_compatible_embeddings(payload, expected=len(texts))


def _embed_azure_openai(texts: list[str], *, config: EmbeddingConfig) -> list[list[float]]:
    base_url = normalize_base_url(str(config.base_url or "")).rstrip("/")
    api_key = str(config.api_key or "")
    deployment = str(config.azure_deployment or "")
    api_version = str(config.azure_api_version or "")

    url = f"{base_url}/openai/deployments/{deployment}/embeddings"
    client = get_llm_http_client()
    resp = client.post(
        url,
        params={"api-version": api_version},
        headers={"api-key": api_key},
        json={"input": texts},
        timeout=float(config.timeout_seconds),
    )
    resp.raise_for_status()
    payload = resp.json()
    return _parse_openai_compatible_embeddings(payload, expected=len(texts))


def _embed_google_gemini(texts: list[str], *, config: EmbeddingConfig) -> list[list[float]]:
    base_url = normalize_base_url(str(config.base_url or "")).rstrip("/")
    model = str(config.model or "")
    api_key = str(config.api_key or "")

    client = get_llm_http_client()
    timeout = float(config.timeout_seconds)

    def post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = client.post(
            url,
            headers={"Content-Type": "application/json", "Accept": "application/json", "x-goog-api-key": api_key},
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()

    if len(texts) == 1:
        url = f"{base_url}/v1beta/models/{model}:embedContent"
        payload = {"content": {"parts": [{"text": texts[0]}]}}
        out = post(url, payload)
        emb = out.get("embedding") if isinstance(out, dict) else None
        values = emb.get("values") if isinstance(emb, dict) else None
        if not isinstance(values, list):
            raise RuntimeError("bad google embeddings response: missing embedding.values")
        return [[float(x) for x in values]]

    url = f"{base_url}/v1beta/models/{model}:batchEmbedContents"
    payload = {"requests": [{"content": {"parts": [{"text": t}]}} for t in texts]}
    out = post(url, payload)
    embeddings = out.get("embeddings") if isinstance(out, dict) else None
    if not isinstance(embeddings, list):
        raise RuntimeError("bad google embeddings response: missing embeddings")

    vectors: list[list[float]] = []
    for item in embeddings:
        values = item.get("values") if isinstance(item, dict) else None
        if not isinstance(values, list):
            continue
        vectors.append([float(x) for x in values])

    if len(vectors) != len(texts):
        raise RuntimeError("bad google embeddings response: length mismatch")
    return vectors


def _embed_sentence_transformers(texts: list[str], *, config: EmbeddingConfig) -> list[list[float]]:
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
    except Exception as exc:
        raise RuntimeError("sentence-transformers dependency missing") from exc

    model_name = str(config.sentence_transformers_model or "").strip()
    if not model_name:
        raise RuntimeError("sentence-transformers model missing")

    cache_dir = str(config.sentence_transformers_cache_dir or "").strip() or None
    requested_device = str(config.sentence_transformers_device or "").strip() or "cpu"

    def get_cached(device: str) -> object | None:
        key = (model_name, cache_dir, device)
        with _ST_MODEL_LOCK:
            return _ST_MODELS.get(key)

    def set_cached(device: str, model: object) -> None:
        key = (model_name, cache_dir, device)
        with _ST_MODEL_LOCK:
            _ST_MODELS[key] = model

    def load_model(device: str) -> object:
        cached = get_cached(device)
        if cached is not None:
            return cached
        kwargs: dict[str, Any] = {}
        if cache_dir:
            kwargs["cache_folder"] = cache_dir
        if device:
            kwargs["device"] = device
        model = SentenceTransformer(model_name, **kwargs)
        set_cached(device, model)
        return model

    try:
        model = load_model(requested_device)
    except Exception as exc:
        if requested_device != "cpu":
            model = load_model("cpu")
        else:
            raise RuntimeError("sentence-transformers model load failed") from exc

    try:
        vectors = model.encode(texts, normalize_embeddings=True)  # type: ignore[no-any-return]
    except TypeError:
        vectors = model.encode(texts)  # type: ignore[no-any-return]

    if hasattr(vectors, "tolist"):
        vectors = vectors.tolist()
    return [[float(x) for x in v] for v in vectors]


def _parse_openai_compatible_embeddings(payload: object, *, expected: int) -> list[list[float]]:
    if not isinstance(payload, dict):
        raise RuntimeError("bad embeddings response: payload not object")
    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError("bad embeddings response: missing data")

    vectors: list[list[float]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        emb = item.get("embedding")
        if not isinstance(emb, list):
            continue
        vec = [float(x) for x in emb]
        vectors.append(vec)

    if len(vectors) != expected:
        raise RuntimeError("bad embeddings response: length mismatch")
    return vectors
