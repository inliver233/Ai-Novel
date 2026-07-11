from __future__ import annotations

import re
from math import isfinite
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine.url import make_url

from app.core.auth_password import PASSWORD_MIN_LENGTH


def _backend_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def _is_abs_path(value: str) -> bool:
    if value.startswith("/"):
        return True
    if value.startswith("\\\\"):
        return True
    return bool(re.match(r"^[A-Za-z]:[\\/]", value))


AppEnv = Literal["dev", "test", "prod"]
LLMContractMode = Literal["audit", "enforce"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
TaskQueueBackend = Literal["rq", "inline"]
CookieSameSite = Literal["lax", "strict", "none"]
VectorBackend = Literal["auto", "chroma", "pgvector"]
VectorChromaCollectionNaming = Literal["legacy", "hash"]
VectorEmbeddingProvider = Literal[
    "openai_compatible",
    "azure_openai",
    "google",
    "custom",
    "local_proxy",
    "sentence_transformers",
]


def _parse_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("value must be a base-10 integer")
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be a base-10 integer") from exc


def _parse_float(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("value must be a finite number")
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be a finite number") from exc
    if not isfinite(parsed):
        raise ValueError("value must be a finite number")
    return parsed


def _required_string(value: object) -> object:
    if value is not None and not isinstance(value, str):
        return value
    raw = (value or "").strip()
    if not raw:
        raise ValueError("value must not be blank")
    return raw


def _optional_string(value: object) -> object:
    if value is not None and not isinstance(value, str):
        return value
    raw = (value or "").strip()
    return raw or None


ParsedInt = Annotated[int, BeforeValidator(_parse_int)]
ParsedFloat = Annotated[float, BeforeValidator(_parse_float)]
RequiredString = Annotated[str, BeforeValidator(_required_string)]
OptionalString = Annotated[str | None, BeforeValidator(_optional_string)]


WEAK_PROD_ADMIN_PASSWORDS = {
    "changeme123!",
    "changeme",
    "password123",
    "password",
    "admin123",
    "admin",
    "12345678",
}


def _is_weak_admin_password(value: str | None) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    if len(raw) < PASSWORD_MIN_LENGTH:
        return True
    return raw.lower() in WEAK_PROD_ADMIN_PASSWORDS


def _normalize_app_env(value: object) -> str:
    raw = str(value).strip().lower()
    aliases = {
        "dev": "dev",
        "development": "dev",
        "test": "test",
        "testing": "test",
        "prod": "prod",
        "production": "prod",
    }
    try:
        return aliases[raw]
    except KeyError as exc:
        raise ValueError("APP_ENV must be 'dev', 'test', or 'prod'") from exc


def _normalize_log_level(value: object) -> str:
    raw = str(value).strip().upper()
    if raw == "WARN":
        raw = "WARNING"
    if raw not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ValueError("LOG_LEVEL must be one of: DEBUG/INFO/WARNING/ERROR")
    return raw


def _normalize_llm_config_mode(value: object) -> str:
    raw = str(value or "").strip().lower()
    aliases = {"audit": "audit", "enforce": "enforce", "strict": "enforce"}
    try:
        return aliases[raw]
    except KeyError as exc:
        raise ValueError("LLM_CONFIG_MODE must be audit or enforce") from exc


def _normalize_cookie_samesite(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw not in ("lax", "strict", "none"):
        raise ValueError("AUTH_COOKIE_SAMESITE must be lax, strict, or none")
    return raw


def _normalize_task_queue_backend(value: object) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "rq": "rq",
        "redis_rq": "rq",
        "inline": "inline",
        "inprocess": "inline",
        "in_process": "inline",
    }
    try:
        return aliases[raw]
    except KeyError as exc:
        raise ValueError("TASK_QUEUE_BACKEND must be 'rq' or 'inline'") from exc


def _normalize_chroma_collection_naming(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw not in ("legacy", "hash"):
        raise ValueError("VECTOR_CHROMA_COLLECTION_NAMING must be legacy or hash")
    return raw


def _normalize_embedding_provider(value: object) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "openai": "openai_compatible",
        "openai_compat": "openai_compatible",
        "openai_compatible": "openai_compatible",
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
    try:
        return aliases[raw]
    except KeyError as exc:
        raise ValueError(
            "VECTOR_EMBEDDING_PROVIDER must be "
            "openai_compatible|azure_openai|google|custom|local_proxy|sentence_transformers"
        ) from exc


def _normalize_database_url(value: object) -> object:
    raw = _required_string(value)
    if not isinstance(raw, str):
        return raw
    try:
        url = make_url(raw)
    except Exception:
        raise ValueError("DATABASE_URL must be a valid SQLAlchemy database URL") from None

    if url.get_backend_name() != "sqlite":
        return raw

    db = str(url.database or "").strip()
    if not db or db == ":memory:" or db.startswith("file:"):
        return raw
    if _is_abs_path(db):
        return raw

    abs_path = (_backend_dir() / db).resolve()
    return str(url.set(database=abs_path.as_posix()))


def _normalize_optional_backend_path(value: object) -> object:
    raw = _optional_string(value)
    if raw is None or not isinstance(raw, str) or _is_abs_path(raw):
        return raw
    return (_backend_dir() / raw).resolve().as_posix()


NormalizedAppEnv = Annotated[AppEnv, BeforeValidator(_normalize_app_env)]
NormalizedLogLevel = Annotated[LogLevel, BeforeValidator(_normalize_log_level)]
NormalizedLLMContractMode = Annotated[LLMContractMode, BeforeValidator(_normalize_llm_config_mode)]
NormalizedCookieSameSite = Annotated[CookieSameSite, BeforeValidator(_normalize_cookie_samesite)]
NormalizedTaskQueueBackend = Annotated[TaskQueueBackend, BeforeValidator(_normalize_task_queue_backend)]
NormalizedChromaCollectionNaming = Annotated[
    VectorChromaCollectionNaming,
    BeforeValidator(_normalize_chroma_collection_naming),
]
NormalizedEmbeddingProvider = Annotated[
    VectorEmbeddingProvider,
    BeforeValidator(_normalize_embedding_provider),
]
NormalizedDatabaseUrl = Annotated[str, BeforeValidator(_normalize_database_url)]
OptionalBackendPath = Annotated[str | None, BeforeValidator(_normalize_optional_backend_path)]


class Settings(BaseSettings):
    app_env: NormalizedAppEnv = "dev"
    log_level: NormalizedLogLevel = "INFO"
    database_url: NormalizedDatabaseUrl = "sqlite:///./ainovel.db"
    db_pool_size: Annotated[ParsedInt, Field(gt=0, le=50)] = 5
    db_max_overflow: Annotated[ParsedInt, Field(ge=0, le=200)] = 10
    db_pool_timeout_seconds: Annotated[ParsedInt, Field(gt=0, le=120)] = 30
    db_pool_recycle_seconds: Annotated[ParsedInt, Field(gt=0, le=24 * 60 * 60)] = 1800
    cors_origins: str = "http://localhost:5173"
    app_version: str = "0.1.0"
    secret_encryption_key: OptionalString = None

    auth_session_signing_key: OptionalString = None
    auth_dev_fallback_user_id: OptionalString = "local-user"
    llm_config_mode: NormalizedLLMContractMode = "audit"
    auth_session_ttl_seconds: Annotated[ParsedInt, Field(gt=0)] = 60 * 60 * 24 * 7
    auth_refresh_threshold_seconds: Annotated[ParsedInt, Field(gt=0)] = 60 * 15
    auth_activity_touch_interval_seconds: Annotated[ParsedInt, Field(gt=0, le=3600)] = 30
    auth_online_window_seconds: Annotated[ParsedInt, Field(gt=0, le=24 * 60 * 60)] = 60 * 5
    auth_cookie_user_id_name: RequiredString = "user_id"
    auth_cookie_expire_at_name: RequiredString = "session_expire_at"
    auth_cookie_samesite: NormalizedCookieSameSite = "lax"
    auth_admin_user_id: OptionalString = None
    auth_admin_password: OptionalString = None
    auth_admin_email: OptionalString = None
    auth_admin_display_name: OptionalString = "管理员"
    auth_bcrypt_rounds: Annotated[ParsedInt, Field(ge=10, le=15)] = 12

    linuxdo_oidc_discovery_url: RequiredString = "https://connect.linux.do/.well-known/openid-configuration"
    linuxdo_oidc_discovery_ttl_seconds: Annotated[ParsedInt, Field(gt=0, le=24 * 60 * 60)] = 300
    linuxdo_oidc_client_id: OptionalString = None
    linuxdo_oidc_client_secret: OptionalString = None
    linuxdo_oidc_scopes: RequiredString = "openid profile email"
    linuxdo_oidc_redirect_uri: OptionalString = None

    task_queue_backend: NormalizedTaskQueueBackend = "rq"
    redis_url: RequiredString = "redis://localhost:6379/0"
    rq_queue_name: RequiredString = "default"
    project_task_heartbeat_interval_seconds: Annotated[ParsedInt, Field(gt=0)] = 5
    project_task_watchdog_enabled: bool = True
    project_task_watchdog_interval_seconds: Annotated[ParsedInt, Field(gt=0)] = 15
    project_task_stale_running_timeout_seconds: Annotated[ParsedInt, Field(gt=0)] = 120
    project_task_queued_reconcile_after_seconds: Annotated[ParsedInt, Field(gt=0)] = 20
    batch_generation_max_count: Annotated[ParsedInt, Field(gt=0)] = 200
    batch_generation_project_active_limit: Annotated[ParsedInt, Field(gt=0)] = 1
    batch_generation_user_active_limit: Annotated[ParsedInt, Field(gt=0)] = 3
    batch_generation_provider_active_limit: Annotated[ParsedInt, Field(gt=0)] = 3

    vector_chroma_persist_dir: OptionalBackendPath = None
    vector_chroma_collection_naming: NormalizedChromaCollectionNaming = "hash"
    vector_embedding_provider: NormalizedEmbeddingProvider = "openai_compatible"
    vector_embedding_base_url: OptionalString = None
    vector_embedding_model: OptionalString = None
    vector_embedding_api_key: OptionalString = None
    vector_embedding_azure_deployment: OptionalString = None
    vector_embedding_azure_api_version: OptionalString = None
    vector_embedding_sentence_transformers_model: OptionalString = None
    vector_embedding_sentence_transformers_cache_dir: OptionalBackendPath = None
    vector_embedding_sentence_transformers_device: OptionalString = None
    vector_backend: VectorBackend = "auto"
    vector_hybrid_enabled: bool = True
    vector_priority_retrieval_enabled: bool = False
    vector_rerank_enabled: bool = False
    vector_rerank_external_base_url: OptionalString = None
    vector_rerank_external_model: OptionalString = None
    vector_rerank_external_api_key: OptionalString = None
    vector_rerank_external_timeout_seconds: Annotated[ParsedFloat, Field(ge=1.0, le=120.0)] = 15.0
    vector_hybrid_rrf_k: Annotated[ParsedInt, Field(gt=0)] = 60
    vector_overfiltering_enabled: bool = True
    vector_max_candidates: Annotated[ParsedInt, Field(gt=0, le=40)] = 20
    vector_final_max_chunks: Annotated[ParsedInt, Field(gt=0, le=12)] = 6
    vector_per_source_id_max_chunks: Annotated[ParsedInt, Field(gt=0)] = 1
    vector_final_char_limit: Annotated[ParsedInt, Field(gt=0, le=20000)] = 6000
    vector_chunk_size: Annotated[ParsedInt, Field(ge=2, le=5000)] = 800
    vector_chunk_overlap: Annotated[ParsedInt, Field(gt=0, le=1000)] = 120
    vector_source_order: OptionalString = None
    vector_source_weights_json: OptionalString = None

    model_config = SettingsConfigDict(
        env_file=str(_backend_dir() / ".env"),
        env_prefix="",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    @model_validator(mode="after")
    def _validate_config(self) -> "Settings":
        if self.app_env == "prod" and not self.secret_encryption_key:
            raise ValueError("SECRET_ENCRYPTION_KEY must be set when APP_ENV=prod")
        if self.app_env == "prod" and self.auth_dev_fallback_user_id:
            raise ValueError("AUTH_DEV_FALLBACK_USER_ID must be empty when APP_ENV=prod")
        if self.app_env == "prod" and self.task_queue_backend != "rq":
            raise ValueError("TASK_QUEUE_BACKEND must be set to 'rq' when APP_ENV=prod")
        if self.app_env == "prod":
            origins = self.cors_origins_list()
            if not origins:
                raise ValueError("CORS_ORIGINS must be configured when APP_ENV=prod")
            if any(origin == "*" for origin in origins):
                raise ValueError("CORS_ORIGINS must not contain '*' when APP_ENV=prod")
            if any(origin.lower() == "null" for origin in origins):
                raise ValueError("CORS_ORIGINS must not contain 'null' when APP_ENV=prod")
            if _is_weak_admin_password(self.auth_admin_password):
                raise ValueError("AUTH_ADMIN_PASSWORD must not use weak or default credentials when APP_ENV=prod")
        if self.task_queue_backend == "rq" and not self.redis_url:
            raise ValueError("REDIS_URL must be set when TASK_QUEUE_BACKEND=rq")
        if self.vector_chunk_overlap >= self.vector_chunk_size:
            raise ValueError("VECTOR_CHUNK_OVERLAP must be less than VECTOR_CHUNK_SIZE")
        return self

    def cors_origins_list(self) -> list[str]:
        raw = self.cors_origins.strip()
        if not raw:
            return []
        return [origin.strip() for origin in raw.split(",") if origin.strip()]

    def is_sqlite(self) -> bool:
        return self.database_url.strip().startswith("sqlite")


settings = Settings()
