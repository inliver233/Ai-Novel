from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.llm_profile import LLMProfile
from app.models.llm_preset import LLMPreset
from app.models.llm_task_preset import LLMTaskPreset
from app.models.project import Project
from app.services.generation_service import PreparedLlmCall
from app.services.llm_contract_service import normalize_base_url_for_provider, normalize_max_tokens_for_provider, normalize_provider_model
from app.services.llm_key_resolver import normalize_header_api_key, resolve_api_key_for_profile
from app.services.llm_task_catalog import is_supported_llm_task


@dataclass(frozen=True, slots=True)
class ResolvedTaskPreset:
    project_id: str
    task_key: str
    source: str
    llm_profile_id: str | None
    llm_call: PreparedLlmCall
    api_key: str


def _parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if item is not None]


def _parse_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _to_prepared_llm_call(row: LLMPreset | LLMTaskPreset) -> PreparedLlmCall:
    stop = _parse_json_list(getattr(row, "stop_json", None))
    extra = _parse_json_dict(getattr(row, "extra_json", None))
    provider, model = normalize_provider_model(str(getattr(row, "provider", "") or ""), str(getattr(row, "model", "") or ""))
    params: dict[str, Any] = {
        "temperature": getattr(row, "temperature", None),
        "top_p": getattr(row, "top_p", None),
        "max_tokens": normalize_max_tokens_for_provider(provider, model, getattr(row, "max_tokens", None)),
        "presence_penalty": getattr(row, "presence_penalty", None),
        "frequency_penalty": getattr(row, "frequency_penalty", None),
        "top_k": getattr(row, "top_k", None),
        "stop": stop,
    }
    return PreparedLlmCall(
        provider=provider,
        model=model,
        base_url=str(normalize_base_url_for_provider(provider, getattr(row, "base_url", None)) or ""),
        timeout_seconds=int(getattr(row, "timeout_seconds", 180) or 180),
        params=params,
        params_json=json.dumps(params, ensure_ascii=False),
        extra=extra,
    )


def get_task_override(db: Session, *, project_id: str, task_key: str) -> LLMTaskPreset | None:
    key = str(task_key or "").strip()
    if not key:
        return None
    if not is_supported_llm_task(key):
        return None
    return db.get(LLMTaskPreset, (project_id, key))


def resolve_task_preset(
    db: Session,
    *,
    project_id: str,
    task_key: str,
) -> tuple[LLMPreset | LLMTaskPreset | None, str]:
    override = get_task_override(db, project_id=project_id, task_key=task_key)
    if override is not None:
        return override, "task_override"
    return db.get(LLMPreset, project_id), "project_default"


def resolve_task_runtime_provider(
    db: Session,
    *,
    project_id: str,
    task_key: str,
) -> str:
    """Resolve the canonical provider from the same preset used at runtime."""

    row, _source = resolve_task_preset(db, project_id=project_id, task_key=task_key)
    if row is None:
        raise AppError(code="LLM_CONFIG_ERROR", message="请先在 Prompts 页保存 LLM 配置", status_code=400)
    provider, _model = normalize_provider_model(str(row.provider or ""), str(row.model or ""))
    provider = str(provider or "").strip().lower()
    if not provider or len(provider) > 64:
        raise AppError(
            code="LLM_CONFIG_ERROR",
            message="LLM provider 配置不合法",
            status_code=400,
            details={"task_key": str(task_key or "").strip()},
        )
    return provider


def resolve_task_llm_config(
    db: Session,
    *,
    project: Project,
    user_id: str,
    task_key: str,
    header_api_key: str | None,
) -> ResolvedTaskPreset | None:
    row, source = resolve_task_preset(db, project_id=project.id, task_key=task_key)
    if row is None:
        return None

    header = normalize_header_api_key(header_api_key)
    llm_profile_id = getattr(row, "llm_profile_id", None) if source == "task_override" else None
    profile_id = str(llm_profile_id or "").strip() or (str(project.llm_profile_id or "").strip() or None)

    if header is not None:
        api_key = header
    elif profile_id is not None:
        profile = db.get(LLMProfile, profile_id)
        if profile is None or profile.owner_user_id != user_id:
            raise AppError(code="LLM_KEY_MISSING", message="请先在 Prompts 页面保存 API Key", status_code=401)
        api_key = resolve_api_key_for_profile(profile=profile, header_api_key=None)
    else:
        raise AppError(code="LLM_KEY_MISSING", message="请先在 Prompts 页面保存 API Key", status_code=401)

    llm_call = _to_prepared_llm_call(row)
    return ResolvedTaskPreset(
        project_id=project.id,
        task_key=str(task_key or "").strip(),
        source=source,
        llm_profile_id=profile_id,
        llm_call=llm_call,
        api_key=api_key,
    )
