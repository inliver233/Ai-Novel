from __future__ import annotations

import json

from fastapi import APIRouter, Request

from app.api.deps import DbDep, UserIdDep, require_project_editor, require_project_viewer
from app.core.config import settings
from app.core.errors import AppError, ok_payload
from app.core.secrets import SecretCryptoError, decrypt_secret, encrypt_secret, mask_api_key
from app.models.project_settings import ProjectSettings
from app.schemas.settings import ProjectSettingsOut, ProjectSettingsUpdate, QueryPreprocessingConfig
from app.services.embedding_service import embedding_enabled_reason, resolve_embedding_config

router = APIRouter()

_VECTOR_DISABLED_BASE_URL_MISSING = "embedding_base_url_missing"
_VECTOR_DISABLED_MODEL_MISSING = "embedding_model_missing"
_VECTOR_DISABLED_API_KEY_MISSING = "embedding_api_key_missing"
_VECTOR_DISABLED_API_KEY_DECRYPT_FAILED = "embedding_api_key_decrypt_failed"


def _parse_query_preprocessing_json(raw: str | None) -> QueryPreprocessingConfig | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    try:
        return QueryPreprocessingConfig.model_validate(data)
    except ValueError:
        return None


def _vector_effective_disabled_reason(
    *,
    provider: str,
    base_url: str,
    model: str,
    has_api_key: bool,
    azure_deployment: str,
    azure_api_version: str,
    sentence_transformers_model: str,
) -> str | None:
    cfg = resolve_embedding_config(
        {
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "api_key": "present" if has_api_key else None,
            "azure_deployment": azure_deployment,
            "azure_api_version": azure_api_version,
            "sentence_transformers_model": sentence_transformers_model,
        }
    )
    enabled, disabled_reason = embedding_enabled_reason(cfg)
    return None if enabled else disabled_reason


def _build_settings_payload(*, project_id: str, row: ProjectSettings | None) -> dict:
    world_setting = (row.world_setting or "") if row is not None else ""
    style_guide = (row.style_guide or "") if row is not None else ""
    constraints = (row.constraints or "") if row is not None else ""
    context_optimizer_enabled = bool(getattr(row, "context_optimizer_enabled", False)) if row is not None else False

    auto_update_worldbook_enabled = bool(getattr(row, "auto_update_worldbook_enabled", True)) if row is not None else True
    auto_update_characters_enabled = (
        bool(getattr(row, "auto_update_characters_enabled", True)) if row is not None else True
    )
    auto_update_story_memory_enabled = (
        bool(getattr(row, "auto_update_story_memory_enabled", True)) if row is not None else True
    )
    auto_update_graph_enabled = bool(getattr(row, "auto_update_graph_enabled", True)) if row is not None else True
    auto_update_vector_enabled = bool(getattr(row, "auto_update_vector_enabled", True)) if row is not None else True
    auto_update_search_enabled = bool(getattr(row, "auto_update_search_enabled", True)) if row is not None else True
    auto_update_fractal_enabled = bool(getattr(row, "auto_update_fractal_enabled", True)) if row is not None else True
    auto_update_tables_enabled = bool(getattr(row, "auto_update_tables_enabled", True)) if row is not None else True

    qp_default = QueryPreprocessingConfig()
    qp_override = _parse_query_preprocessing_json((row.query_preprocessing_json or "").strip() if row is not None else None)
    qp_effective = qp_override or qp_default
    qp_source = "project" if qp_override is not None else "default"

    rerank_override_enabled = row.vector_rerank_enabled if row is not None else None
    rerank_override_method_raw = (row.vector_rerank_method or "").strip() if row is not None else ""
    rerank_override_method = rerank_override_method_raw or None
    rerank_override_top_k = row.vector_rerank_top_k if row is not None else None

    rerank_default_enabled = bool(getattr(settings, "vector_rerank_enabled", False))
    rerank_default_method = "auto"
    rerank_default_top_k = int(getattr(settings, "vector_max_candidates", 20) or 20)

    rerank_effective_enabled = rerank_override_enabled if rerank_override_enabled is not None else rerank_default_enabled
    rerank_effective_method = rerank_override_method or rerank_default_method
    rerank_effective_top_k = rerank_override_top_k if rerank_override_top_k is not None else rerank_default_top_k

    source_project_fields = {
        "enabled": rerank_override_enabled is not None,
        "method": rerank_override_method is not None,
        "top_k": rerank_override_top_k is not None,
    }
    source_default_fields = {
        "enabled": rerank_override_enabled is None,
        "method": rerank_override_method is None,
        "top_k": rerank_override_top_k is None,
    }
    if any(source_project_fields.values()) and any(source_default_fields.values()):
        rerank_effective_source = "mixed"
    elif any(source_project_fields.values()):
        rerank_effective_source = "project"
    else:
        rerank_effective_source = "default"

    rerank_override_provider = (getattr(row, "vector_rerank_provider", None) or "").strip() if row is not None else ""
    rerank_override_base_url = (getattr(row, "vector_rerank_base_url", None) or "").strip() if row is not None else ""
    rerank_override_model = (getattr(row, "vector_rerank_model", None) or "").strip() if row is not None else ""
    rerank_override_timeout_seconds = getattr(row, "vector_rerank_timeout_seconds", None) if row is not None else None
    rerank_override_hybrid_alpha = getattr(row, "vector_rerank_hybrid_alpha", None) if row is not None else None
    rerank_override_ciphertext = getattr(row, "vector_rerank_api_key_ciphertext", None) if row is not None else None
    rerank_override_masked = (getattr(row, "vector_rerank_api_key_masked", None) or "").strip() if row is not None else ""
    rerank_override_has_api_key = bool(str(rerank_override_ciphertext or "").strip())

    env_rerank_provider = "external_rerank_api"
    env_rerank_base_url = str(getattr(settings, "vector_rerank_external_base_url", "") or "").strip()
    env_rerank_model = str(getattr(settings, "vector_rerank_external_model", "") or "").strip()
    env_rerank_api_key = str(getattr(settings, "vector_rerank_external_api_key", "") or "").strip()
    env_rerank_timeout_seconds_raw = float(getattr(settings, "vector_rerank_external_timeout_seconds", 15.0) or 15.0)
    env_rerank_timeout_seconds = int(max(1.0, min(env_rerank_timeout_seconds_raw, 120.0)))
    env_rerank_has_api_key = bool(env_rerank_api_key)
    env_rerank_masked_api_key = mask_api_key(env_rerank_api_key) if env_rerank_api_key else ""

    rerank_effective_provider = rerank_override_provider or (env_rerank_provider if env_rerank_base_url else "")
    rerank_effective_base_url = rerank_override_base_url or env_rerank_base_url
    rerank_effective_model = rerank_override_model or env_rerank_model
    rerank_effective_timeout_seconds = int(rerank_override_timeout_seconds) if rerank_override_timeout_seconds is not None else env_rerank_timeout_seconds
    rerank_effective_hybrid_alpha = float(rerank_override_hybrid_alpha) if rerank_override_hybrid_alpha is not None else 0.0
    rerank_effective_has_api_key = rerank_override_has_api_key or env_rerank_has_api_key
    rerank_effective_masked_api_key = rerank_override_masked if rerank_override_has_api_key else env_rerank_masked_api_key

    rerank_config_project_fields = {
        "provider": bool(rerank_override_provider),
        "base_url": bool(rerank_override_base_url),
        "model": bool(rerank_override_model),
        "timeout_seconds": rerank_override_timeout_seconds is not None,
        "hybrid_alpha": rerank_override_hybrid_alpha is not None,
        "api_key": rerank_override_has_api_key,
    }
    rerank_config_default_fields = {k: not v for k, v in rerank_config_project_fields.items()}
    if any(rerank_config_project_fields.values()) and any(rerank_config_default_fields.values()):
        rerank_effective_config_source = "mixed"
    elif any(rerank_config_project_fields.values()):
        rerank_effective_config_source = "project"
    else:
        rerank_effective_config_source = "default"

    override_provider = (row.vector_embedding_provider or "").strip() if row is not None else ""
    override_base_url = (row.vector_embedding_base_url or "").strip() if row is not None else ""
    override_model = (row.vector_embedding_model or "").strip() if row is not None else ""
    override_azure_deployment = (row.vector_embedding_azure_deployment or "").strip() if row is not None else ""
    override_azure_api_version = (row.vector_embedding_azure_api_version or "").strip() if row is not None else ""
    override_st_model = (row.vector_embedding_sentence_transformers_model or "").strip() if row is not None else ""
    override_ciphertext = row.vector_embedding_api_key_ciphertext if row is not None else None
    override_masked = (row.vector_embedding_api_key_masked or "").strip() if row is not None else ""
    override_has_api_key = bool(override_ciphertext)

    env_provider = str(getattr(settings, "vector_embedding_provider", "openai_compatible") or "openai_compatible").strip()
    env_base_url = str(settings.vector_embedding_base_url or "").strip()
    env_model = str(settings.vector_embedding_model or "").strip()
    env_azure_deployment = str(getattr(settings, "vector_embedding_azure_deployment", "") or "").strip()
    env_azure_api_version = str(getattr(settings, "vector_embedding_azure_api_version", "") or "").strip()
    env_st_model = str(getattr(settings, "vector_embedding_sentence_transformers_model", "") or "").strip()
    env_api_key = str(settings.vector_embedding_api_key or "").strip()
    env_has_api_key = bool(env_api_key)
    env_masked = mask_api_key(env_api_key) if env_api_key else ""

    override_api_key_ok = False
    if override_ciphertext:
        try:
            _ = decrypt_secret(override_ciphertext)
            override_api_key_ok = True
        except SecretCryptoError:
            override_api_key_ok = False

    effective_provider = override_provider or env_provider or "openai_compatible"
    effective_base_url = override_base_url or env_base_url
    effective_model = override_model or env_model
    effective_azure_deployment = override_azure_deployment or env_azure_deployment
    effective_azure_api_version = override_azure_api_version or env_azure_api_version
    effective_st_model = override_st_model or env_st_model
    effective_has_api_key = override_api_key_ok or env_has_api_key
    effective_masked = override_masked if override_api_key_ok else env_masked

    project_fields: list[str] = []
    env_fields: list[str] = []

    if override_provider:
        project_fields.append("provider")
    elif env_provider and env_provider != "openai_compatible":
        env_fields.append("provider")

    if effective_base_url:
        (project_fields if override_base_url else env_fields).append("base_url")
    if effective_model:
        (project_fields if override_model else env_fields).append("model")
    if effective_azure_deployment:
        (project_fields if override_azure_deployment else env_fields).append("azure_deployment")
    if effective_azure_api_version:
        (project_fields if override_azure_api_version else env_fields).append("azure_api_version")
    if effective_st_model:
        (project_fields if override_st_model else env_fields).append("sentence_transformers_model")
    if effective_has_api_key:
        (project_fields if override_api_key_ok else env_fields).append("api_key")

    if project_fields and env_fields:
        effective_source = "mixed"
    elif project_fields:
        effective_source = "project"
    elif env_fields:
        effective_source = "env"
    else:
        effective_source = "none"

    disabled_reason = _vector_effective_disabled_reason(
        provider=effective_provider,
        base_url=effective_base_url,
        model=effective_model,
        has_api_key=effective_has_api_key,
        azure_deployment=effective_azure_deployment,
        azure_api_version=effective_azure_api_version,
        sentence_transformers_model=effective_st_model,
    )
    if disabled_reason is None and override_has_api_key and not override_api_key_ok and not env_has_api_key:
        disabled_reason = _VECTOR_DISABLED_API_KEY_DECRYPT_FAILED

    payload = ProjectSettingsOut(
        project_id=project_id,
        world_setting=world_setting,
        style_guide=style_guide,
        constraints=constraints,
        context_optimizer_enabled=context_optimizer_enabled,
        auto_update_worldbook_enabled=auto_update_worldbook_enabled,
        auto_update_characters_enabled=auto_update_characters_enabled,
        auto_update_story_memory_enabled=auto_update_story_memory_enabled,
        auto_update_graph_enabled=auto_update_graph_enabled,
        auto_update_vector_enabled=auto_update_vector_enabled,
        auto_update_search_enabled=auto_update_search_enabled,
        auto_update_fractal_enabled=auto_update_fractal_enabled,
        auto_update_tables_enabled=auto_update_tables_enabled,
        query_preprocessing=qp_override,
        query_preprocessing_default=qp_default,
        query_preprocessing_effective=qp_effective,
        query_preprocessing_effective_source=qp_source,
        vector_rerank_enabled=rerank_override_enabled,
        vector_rerank_method=rerank_override_method,
        vector_rerank_top_k=rerank_override_top_k,
        vector_rerank_provider=rerank_override_provider,
        vector_rerank_base_url=rerank_override_base_url,
        vector_rerank_model=rerank_override_model,
        vector_rerank_timeout_seconds=rerank_override_timeout_seconds,
        vector_rerank_hybrid_alpha=rerank_override_hybrid_alpha,
        vector_rerank_has_api_key=rerank_override_has_api_key,
        vector_rerank_masked_api_key=rerank_override_masked,
        vector_rerank_effective_enabled=rerank_effective_enabled,
        vector_rerank_effective_method=rerank_effective_method,
        vector_rerank_effective_top_k=rerank_effective_top_k,
        vector_rerank_effective_source=rerank_effective_source,
        vector_rerank_effective_provider=rerank_effective_provider,
        vector_rerank_effective_base_url=rerank_effective_base_url,
        vector_rerank_effective_model=rerank_effective_model,
        vector_rerank_effective_timeout_seconds=rerank_effective_timeout_seconds,
        vector_rerank_effective_hybrid_alpha=rerank_effective_hybrid_alpha,
        vector_rerank_effective_has_api_key=rerank_effective_has_api_key,
        vector_rerank_effective_masked_api_key=rerank_effective_masked_api_key,
        vector_rerank_effective_config_source=rerank_effective_config_source,
        vector_embedding_provider=override_provider,
        vector_embedding_base_url=override_base_url,
        vector_embedding_model=override_model,
        vector_embedding_azure_deployment=override_azure_deployment,
        vector_embedding_azure_api_version=override_azure_api_version,
        vector_embedding_sentence_transformers_model=override_st_model,
        vector_embedding_has_api_key=override_has_api_key,
        vector_embedding_masked_api_key=override_masked,
        vector_embedding_effective_provider=effective_provider,
        vector_embedding_effective_base_url=effective_base_url,
        vector_embedding_effective_model=effective_model,
        vector_embedding_effective_azure_deployment=effective_azure_deployment,
        vector_embedding_effective_azure_api_version=effective_azure_api_version,
        vector_embedding_effective_sentence_transformers_model=effective_st_model,
        vector_embedding_effective_has_api_key=effective_has_api_key,
        vector_embedding_effective_masked_api_key=effective_masked,
        vector_embedding_effective_disabled_reason=disabled_reason,
        vector_embedding_effective_source=effective_source,
    ).model_dump()
    return payload


@router.get("/projects/{project_id}/settings")
def get_settings(request: Request, db: DbDep, user_id: UserIdDep, project_id: str) -> dict:
    request_id = request.state.request_id
    require_project_viewer(db, project_id=project_id, user_id=user_id)
    row = db.get(ProjectSettings, project_id)
    payload = _build_settings_payload(project_id=project_id, row=row)
    return ok_payload(request_id=request_id, data={"settings": payload})


@router.put("/projects/{project_id}/settings")
def put_settings(request: Request, db: DbDep, user_id: UserIdDep, project_id: str, body: ProjectSettingsUpdate) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)
    row = db.get(ProjectSettings, project_id)
    if row is None:
        row = ProjectSettings(project_id=project_id, world_setting="", style_guide="", constraints="")
        db.add(row)

    if body.world_setting is not None:
        row.world_setting = body.world_setting
    if body.style_guide is not None:
        row.style_guide = body.style_guide
    if body.constraints is not None:
        row.constraints = body.constraints

    if "context_optimizer_enabled" in body.model_fields_set:
        row.context_optimizer_enabled = bool(body.context_optimizer_enabled)

    if "auto_update_worldbook_enabled" in body.model_fields_set and body.auto_update_worldbook_enabled is not None:
        row.auto_update_worldbook_enabled = bool(body.auto_update_worldbook_enabled)
    if "auto_update_characters_enabled" in body.model_fields_set and body.auto_update_characters_enabled is not None:
        row.auto_update_characters_enabled = bool(body.auto_update_characters_enabled)
    if "auto_update_story_memory_enabled" in body.model_fields_set and body.auto_update_story_memory_enabled is not None:
        row.auto_update_story_memory_enabled = bool(body.auto_update_story_memory_enabled)
    if "auto_update_graph_enabled" in body.model_fields_set and body.auto_update_graph_enabled is not None:
        row.auto_update_graph_enabled = bool(body.auto_update_graph_enabled)
    if "auto_update_vector_enabled" in body.model_fields_set and body.auto_update_vector_enabled is not None:
        row.auto_update_vector_enabled = bool(body.auto_update_vector_enabled)
    if "auto_update_search_enabled" in body.model_fields_set and body.auto_update_search_enabled is not None:
        row.auto_update_search_enabled = bool(body.auto_update_search_enabled)
    if "auto_update_fractal_enabled" in body.model_fields_set and body.auto_update_fractal_enabled is not None:
        row.auto_update_fractal_enabled = bool(body.auto_update_fractal_enabled)
    if "auto_update_tables_enabled" in body.model_fields_set and body.auto_update_tables_enabled is not None:
        row.auto_update_tables_enabled = bool(body.auto_update_tables_enabled)

    if "query_preprocessing" in body.model_fields_set:
        if body.query_preprocessing is None:
            row.query_preprocessing_json = None
        else:
            row.query_preprocessing_json = json.dumps(
                body.query_preprocessing.model_dump(),
                ensure_ascii=False,
                separators=(",", ":"),
            )

    if "vector_rerank_enabled" in body.model_fields_set:
        row.vector_rerank_enabled = body.vector_rerank_enabled

    if "vector_rerank_method" in body.model_fields_set:
        if body.vector_rerank_method is None:
            row.vector_rerank_method = None
        else:
            row.vector_rerank_method = body.vector_rerank_method.strip() or None

    if "vector_rerank_top_k" in body.model_fields_set:
        row.vector_rerank_top_k = int(body.vector_rerank_top_k) if body.vector_rerank_top_k is not None else None

    if "vector_rerank_provider" in body.model_fields_set:
        if body.vector_rerank_provider is None:
            row.vector_rerank_provider = None
        else:
            row.vector_rerank_provider = body.vector_rerank_provider.strip() or None

    if "vector_rerank_base_url" in body.model_fields_set:
        if body.vector_rerank_base_url is None:
            row.vector_rerank_base_url = None
        else:
            row.vector_rerank_base_url = body.vector_rerank_base_url.strip() or None

    if "vector_rerank_model" in body.model_fields_set:
        if body.vector_rerank_model is None:
            row.vector_rerank_model = None
        else:
            row.vector_rerank_model = body.vector_rerank_model.strip() or None

    if "vector_rerank_timeout_seconds" in body.model_fields_set:
        row.vector_rerank_timeout_seconds = int(body.vector_rerank_timeout_seconds) if body.vector_rerank_timeout_seconds is not None else None

    if "vector_rerank_hybrid_alpha" in body.model_fields_set:
        row.vector_rerank_hybrid_alpha = float(body.vector_rerank_hybrid_alpha) if body.vector_rerank_hybrid_alpha is not None else None

    if body.vector_rerank_api_key is not None:
        raw = body.vector_rerank_api_key.strip()
        if not raw:
            row.vector_rerank_api_key_ciphertext = None
            row.vector_rerank_api_key_masked = None
        else:
            try:
                row.vector_rerank_api_key_ciphertext = encrypt_secret(raw)
                row.vector_rerank_api_key_masked = mask_api_key(raw)
            except SecretCryptoError as exc:
                raise AppError.validation(
                    message=str(exc),
                    details={"field": "vector_rerank_api_key"},
                ) from exc

    if body.vector_embedding_provider is not None:
        row.vector_embedding_provider = body.vector_embedding_provider.strip() or None
    if body.vector_embedding_base_url is not None:
        row.vector_embedding_base_url = body.vector_embedding_base_url.strip() or None
    if body.vector_embedding_model is not None:
        row.vector_embedding_model = body.vector_embedding_model.strip() or None
    if body.vector_embedding_azure_deployment is not None:
        row.vector_embedding_azure_deployment = body.vector_embedding_azure_deployment.strip() or None
    if body.vector_embedding_azure_api_version is not None:
        row.vector_embedding_azure_api_version = body.vector_embedding_azure_api_version.strip() or None
    if body.vector_embedding_sentence_transformers_model is not None:
        row.vector_embedding_sentence_transformers_model = body.vector_embedding_sentence_transformers_model.strip() or None
    if body.vector_embedding_api_key is not None:
        raw = body.vector_embedding_api_key.strip()
        if not raw:
            row.vector_embedding_api_key_ciphertext = None
            row.vector_embedding_api_key_masked = None
        else:
            try:
                row.vector_embedding_api_key_ciphertext = encrypt_secret(raw)
                row.vector_embedding_api_key_masked = mask_api_key(raw)
            except SecretCryptoError as exc:
                raise AppError.validation(
                    message=str(exc),
                    details={"field": "vector_embedding_api_key"},
                ) from exc

    db.commit()
    db.refresh(row)
    payload = _build_settings_payload(project_id=project_id, row=row)
    return ok_payload(request_id=request_id, data={"settings": payload})
