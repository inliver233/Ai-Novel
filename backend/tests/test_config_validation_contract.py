from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.core.config import Settings


_BACKEND_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND_DIR.parent
_CONFIG_FILE = _BACKEND_DIR / "app" / "core" / "config.py"

_NUMERIC_CASES = [
    ("db_pool_size", (("1", 1), ("50", 50)), ("0", "51")),
    ("db_max_overflow", (("0", 0), ("200", 200)), ("-1", "201")),
    ("db_pool_timeout_seconds", (("1", 1), ("120", 120)), ("0", "121")),
    ("db_pool_recycle_seconds", (("1", 1), ("86400", 86400)), ("0", "86401")),
    ("auth_session_ttl_seconds", (("1", 1),), ("0",)),
    ("auth_refresh_threshold_seconds", (("1", 1),), ("0",)),
    ("auth_activity_touch_interval_seconds", (("1", 1), ("3600", 3600)), ("0", "3601")),
    ("auth_online_window_seconds", (("1", 1), ("86400", 86400)), ("0", "86401")),
    ("auth_bcrypt_rounds", (("10", 10), ("15", 15)), ("9", "16")),
    ("project_task_heartbeat_interval_seconds", (("1", 1),), ("0",)),
    ("project_task_watchdog_interval_seconds", (("1", 1),), ("0",)),
    ("project_task_stale_running_timeout_seconds", (("1", 1),), ("0",)),
    ("project_task_queued_reconcile_after_seconds", (("1", 1),), ("0",)),
    ("batch_generation_max_count", (("1", 1),), ("0",)),
    ("batch_generation_project_active_limit", (("1", 1),), ("0",)),
    ("batch_generation_user_active_limit", (("1", 1),), ("0",)),
    ("batch_generation_provider_active_limit", (("1", 1),), ("0",)),
    (
        "vector_rerank_external_timeout_seconds",
        (("1.0", 1.0), ("120.0", 120.0)),
        ("0.99", "121", "nan", "inf", "+inf", "-inf"),
    ),
    ("vector_hybrid_rrf_k", (("1", 1),), ("0",)),
    ("vector_max_candidates", (("1", 1), ("40", 40)), ("0", "41")),
    ("vector_final_max_chunks", (("1", 1), ("12", 12)), ("0", "13")),
    ("vector_per_source_id_max_chunks", (("1", 1),), ("0",)),
    ("vector_final_char_limit", (("1", 1), ("20000", 20000)), ("0", "20001")),
    ("vector_chunk_size", (("2", 2), ("5000", 5000)), ("1", "5001")),
    ("vector_chunk_overlap", (("1", 1), ("1000", 1000)), ("0", "1001")),
]

_NUMERIC_FIELDS = {case[0] for case in _NUMERIC_CASES}
_INTEGER_FIELDS = _NUMERIC_FIELDS - {"vector_rerank_external_timeout_seconds"}
_MALFORMED_NUMERIC_CASES = [
    *((field_name, invalid) for field_name in sorted(_NUMERIC_FIELDS) for invalid in ("", "not-a-number")),
    *((field_name, "1.0") for field_name in sorted(_INTEGER_FIELDS)),
]

_NUMERIC_DEFAULTS = {
    "db_pool_size": 5,
    "db_max_overflow": 10,
    "db_pool_timeout_seconds": 30,
    "db_pool_recycle_seconds": 1800,
    "auth_session_ttl_seconds": 604800,
    "auth_refresh_threshold_seconds": 900,
    "auth_activity_touch_interval_seconds": 30,
    "auth_online_window_seconds": 300,
    "auth_bcrypt_rounds": 12,
    "project_task_heartbeat_interval_seconds": 5,
    "project_task_watchdog_interval_seconds": 15,
    "project_task_stale_running_timeout_seconds": 120,
    "project_task_queued_reconcile_after_seconds": 20,
    "batch_generation_max_count": 200,
    "batch_generation_project_active_limit": 1,
    "batch_generation_user_active_limit": 3,
    "batch_generation_provider_active_limit": 3,
    "vector_rerank_external_timeout_seconds": 15.0,
    "vector_hybrid_rrf_k": 60,
    "vector_max_candidates": 20,
    "vector_final_max_chunks": 6,
    "vector_per_source_id_max_chunks": 1,
    "vector_final_char_limit": 6000,
    "vector_chunk_size": 800,
    "vector_chunk_overlap": 120,
}


def _clear_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)


def _settings_from_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> Settings:
    _clear_settings_env(monkeypatch)
    for name, value in values.items():
        monkeypatch.setenv(name.upper(), value)
    return Settings(_env_file=None)


@pytest.mark.parametrize(("field_name", "valid_values", "invalid_values"), _NUMERIC_CASES)
def test_numeric_settings_read_real_environment_and_enforce_bounds(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    valid_values: tuple[tuple[str, int | float], ...],
    invalid_values: tuple[str, ...],
) -> None:
    for valid, expected in valid_values:
        env_values = {field_name: f"  {valid}  "}
        if field_name == "vector_chunk_size" and int(valid) <= 120:
            env_values["vector_chunk_overlap"] = "1"
        if field_name == "vector_chunk_overlap" and int(valid) >= 800:
            env_values["vector_chunk_size"] = "5000"
        configured = _settings_from_env(monkeypatch, **env_values)
        assert getattr(configured, field_name) == expected

    for invalid in invalid_values:
        with pytest.raises(ValidationError):
            _settings_from_env(monkeypatch, **{field_name: invalid})


@pytest.mark.parametrize(("field_name", "invalid"), _MALFORMED_NUMERIC_CASES)
def test_explicit_blank_or_malformed_numeric_environment_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    invalid: str,
) -> None:
    with pytest.raises(ValidationError):
        _settings_from_env(monkeypatch, **{field_name: invalid})


def test_unset_numeric_environment_uses_declared_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    configured = _settings_from_env(monkeypatch)

    assert _NUMERIC_DEFAULTS.keys() == _NUMERIC_FIELDS
    for field_name, expected in _NUMERIC_DEFAULTS.items():
        assert getattr(configured, field_name) == expected


@pytest.mark.parametrize(
    ("field_name", "alias", "expected"),
    [
        ("app_env", "testing", "test"),
        ("log_level", "warn", "WARNING"),
        ("llm_config_mode", "strict", "enforce"),
        ("task_queue_backend", "redis_rq", "rq"),
        ("task_queue_backend", "in_process", "inline"),
        ("vector_embedding_provider", "openai", "openai_compatible"),
        ("vector_embedding_provider", "azure", "azure_openai"),
        ("vector_embedding_provider", "gemini", "google"),
        ("vector_embedding_provider", "st", "sentence_transformers"),
    ],
)
def test_documented_environment_aliases_remain_supported(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    alias: str,
    expected: str,
) -> None:
    configured = _settings_from_env(monkeypatch, **{field_name: alias})
    assert getattr(configured, field_name) == expected


@pytest.mark.parametrize(
    "field_name",
    [
        "app_env",
        "log_level",
        "llm_config_mode",
        "auth_cookie_samesite",
        "task_queue_backend",
        "vector_chroma_collection_naming",
        "vector_embedding_provider",
        "vector_backend",
    ],
)
@pytest.mark.parametrize("invalid", ["", "unsupported-value"])
def test_explicit_blank_or_invalid_enum_environment_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    invalid: str,
) -> None:
    with pytest.raises(ValidationError):
        _settings_from_env(monkeypatch, **{field_name: invalid})


@pytest.mark.parametrize(
    "field_name",
    [
        "database_url",
        "auth_cookie_user_id_name",
        "auth_cookie_expire_at_name",
        "linuxdo_oidc_discovery_url",
        "linuxdo_oidc_scopes",
        "redis_url",
        "rq_queue_name",
    ],
)
def test_explicit_blank_required_string_environment_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
) -> None:
    with pytest.raises(ValidationError):
        _settings_from_env(monkeypatch, **{field_name: "  "})


@pytest.mark.parametrize(
    "field_name",
    [
        "database_url",
        "auth_cookie_user_id_name",
        "auth_admin_email",
        "vector_chroma_persist_dir",
    ],
)
def test_string_normalizers_leave_non_strings_for_pydantic_to_reject(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
) -> None:
    _clear_settings_env(monkeypatch)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field_name: 123})


@pytest.mark.parametrize(
    "field_name",
    [
        "secret_encryption_key",
        "auth_session_signing_key",
        "auth_dev_fallback_user_id",
        "auth_admin_user_id",
        "auth_admin_password",
        "auth_admin_email",
        "auth_admin_display_name",
        "linuxdo_oidc_client_id",
        "linuxdo_oidc_client_secret",
        "linuxdo_oidc_redirect_uri",
        "vector_chroma_persist_dir",
        "vector_embedding_base_url",
        "vector_embedding_model",
        "vector_embedding_api_key",
        "vector_embedding_azure_deployment",
        "vector_embedding_azure_api_version",
        "vector_embedding_sentence_transformers_model",
        "vector_embedding_sentence_transformers_cache_dir",
        "vector_embedding_sentence_transformers_device",
        "vector_rerank_external_base_url",
        "vector_rerank_external_model",
        "vector_rerank_external_api_key",
        "vector_source_order",
        "vector_source_weights_json",
    ],
)
def test_explicit_blank_optional_string_environment_normalizes_to_none(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
) -> None:
    configured = _settings_from_env(monkeypatch, **{field_name: "  "})
    assert getattr(configured, field_name) is None


def test_chunk_overlap_must_be_smaller_than_chunk_size(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="VECTOR_CHUNK_OVERLAP"):
        _settings_from_env(monkeypatch, vector_chunk_size="120", vector_chunk_overlap="120")


def test_malformed_database_url_fails_without_echoing_rendered_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    malformed_dsn = "postgresql://user:dsn-secret-marker@localhost:not-a-port/database"

    with pytest.raises(ValidationError) as exc_info:
        _settings_from_env(monkeypatch, database_url=malformed_dsn)

    rendered = str(exc_info.value)
    assert "DATABASE_URL must be a valid SQLAlchemy database URL" in rendered
    assert malformed_dsn not in rendered
    assert "dsn-secret-marker" not in rendered


def test_rendered_model_validation_error_hides_environment_values(monkeypatch: pytest.MonkeyPatch) -> None:
    weak_password = "S3cr3t!"
    _clear_settings_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", "encryption-key-marker")
    monkeypatch.setenv("AUTH_DEV_FALLBACK_USER_ID", "")
    monkeypatch.setenv("AUTH_ADMIN_PASSWORD", weak_password)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    rendered = str(exc_info.value)
    assert "AUTH_ADMIN_PASSWORD" in rendered
    assert weak_password not in rendered
    assert "encryption-key-marker" not in rendered


def test_config_uses_reusable_annotated_types_without_field_validator_copying() -> None:
    tree = ast.parse(_CONFIG_FILE.read_text(encoding="utf-8"))
    settings_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Settings")
    field_validators = [
        decorator
        for node in settings_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if "field_validator" in ast.unparse(decorator)
    ]
    numeric_fields = {
        node.target.id
        for node in settings_class.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and ("ParsedInt" in ast.unparse(node.annotation) or "ParsedFloat" in ast.unparse(node.annotation))
    }

    assert field_validators == []
    assert numeric_fields == _NUMERIC_FIELDS


def test_environment_templates_satisfy_the_strict_settings_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_settings_env(monkeypatch)
    backend_template = Settings(_env_file=_BACKEND_DIR / ".env.example")
    assert backend_template.vector_chunk_overlap < backend_template.vector_chunk_size

    compose = yaml.safe_load((_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    environment = compose["x-backend-environment"]
    numeric_defaults = {
        "db_pool_size": "5",
        "db_max_overflow": "10",
        "db_pool_timeout_seconds": "30",
        "db_pool_recycle_seconds": "1800",
        "auth_activity_touch_interval_seconds": "30",
        "auth_online_window_seconds": "300",
    }
    for field_name, expected_default in numeric_defaults.items():
        compose_value = environment[field_name.upper()]
        assert compose_value.endswith(f":-{expected_default}}}")
        assert getattr(Settings(_env_file=None, **{field_name: expected_default}), field_name) == int(expected_default)

    root_template_values = {
        line.partition("=")[0].strip(): line.partition("=")[2].strip()
        for line in (_REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    for field_name in ("db_pool_size", "db_max_overflow", "db_pool_timeout_seconds", "db_pool_recycle_seconds"):
        env_name = field_name.upper()
        assert root_template_values[env_name] == numeric_defaults[field_name]

    database_url = environment["DATABASE_URL"]
    assert database_url.startswith("${DATABASE_URL:-postgresql+psycopg2://")
