from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.deps import DbDep, UserIdDep, require_owned_llm_profile, require_project_editor, require_project_viewer
from app.core.errors import AppError, ok_payload
from app.models.llm_task_preset import LLMTaskPreset
from app.schemas.llm_task_presets import LLMTaskPresetOut, LLMTaskPresetPutRequest
from app.services.llm_contract_service import capability_contract, normalize_base_url_for_provider, normalize_max_tokens_for_provider, normalize_provider_model
from app.services.llm_profile_template import decode_extra_json, decode_stop_json, encode_extra_json, encode_stop_json
from app.services.llm_task_catalog import LLM_TASK_CATALOG, is_supported_llm_task

router = APIRouter()


def _to_out(row: LLMTaskPreset) -> dict:
    meta = capability_contract(str(row.provider or "").strip(), str(row.model or "").strip())
    return LLMTaskPresetOut(
        project_id=row.project_id,
        task_key=row.task_key,
        llm_profile_id=row.llm_profile_id,
        provider=meta["provider"],
        provider_key=meta["provider_key"],
        model_key=meta["model_key"],
        known_model=bool(meta["known_model"]),
        contract_mode=str(meta["contract_mode"]),
        pricing=dict(meta.get("pricing") or {}),
        base_url=row.base_url,
        model=meta["model"],
        temperature=row.temperature,
        top_p=row.top_p,
        max_tokens=normalize_max_tokens_for_provider(meta["provider"], meta["model"], row.max_tokens),
        max_tokens_limit=meta["max_tokens_limit"],
        max_tokens_recommended=meta["max_tokens_recommended"],
        context_window_limit=meta["context_window_limit"],
        presence_penalty=row.presence_penalty,
        frequency_penalty=row.frequency_penalty,
        top_k=row.top_k,
        stop=decode_stop_json(row.stop_json),
        timeout_seconds=row.timeout_seconds,
        extra=decode_extra_json(row.extra_json),
        source="task_override",
    ).model_dump()


def _catalog_out() -> list[dict]:
    return [
        {
            "key": item.key,
            "label": item.label,
            "group": item.group,
            "description": item.description,
        }
        for item in LLM_TASK_CATALOG
    ]


@router.get("/projects/{project_id}/llm_task_presets")
def list_llm_task_presets(request: Request, db: DbDep, user_id: UserIdDep, project_id: str) -> dict:
    request_id = request.state.request_id
    require_project_viewer(db, project_id=project_id, user_id=user_id)
    rows = (
        db.execute(select(LLMTaskPreset).where(LLMTaskPreset.project_id == project_id).order_by(LLMTaskPreset.task_key.asc()))
        .scalars()
        .all()
    )
    return ok_payload(
        request_id=request_id,
        data={
            "catalog": _catalog_out(),
            "task_presets": [_to_out(row) for row in rows],
        },
    )


@router.put("/projects/{project_id}/llm_task_presets/{task_key}")
def put_llm_task_preset(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    task_key: str,
    body: LLMTaskPresetPutRequest,
) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)

    task_key_norm = str(task_key or "").strip()
    if not task_key_norm or not is_supported_llm_task(task_key_norm):
        raise AppError.validation(message=f"不支持的 task_key: {task_key_norm or '(empty)'}")

    provider, model = normalize_provider_model(body.provider, body.model)
    profile_id = body.llm_profile_id
    if profile_id:
        profile = require_owned_llm_profile(db, profile_id=profile_id, user_id=user_id)
        profile_provider, _ = normalize_provider_model(profile.provider, profile.model)
        if profile_provider != provider:
            raise AppError(
                code="LLM_CONFIG_ERROR",
                message="任务模块 provider 必须与所选 API 配置 provider 一致",
                status_code=400,
            )

    base_url = normalize_base_url_for_provider(provider, body.base_url)

    row = db.get(LLMTaskPreset, (project_id, task_key_norm))
    if row is None:
        row = LLMTaskPreset(project_id=project_id, task_key=task_key_norm, provider=provider, model=model)
        db.add(row)

    row.llm_profile_id = profile_id
    row.provider = provider
    row.base_url = base_url
    row.model = model
    row.temperature = body.temperature
    row.top_p = body.top_p
    row.max_tokens = normalize_max_tokens_for_provider(provider, model, body.max_tokens)
    row.presence_penalty = body.presence_penalty
    row.frequency_penalty = body.frequency_penalty
    row.top_k = body.top_k
    row.stop_json = encode_stop_json(body.stop)
    row.timeout_seconds = body.timeout_seconds
    row.extra_json = encode_extra_json(body.extra)

    db.commit()
    db.refresh(row)
    return ok_payload(request_id=request_id, data={"task_preset": _to_out(row)})


@router.delete("/projects/{project_id}/llm_task_presets/{task_key}")
def delete_llm_task_preset(request: Request, db: DbDep, user_id: UserIdDep, project_id: str, task_key: str) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)

    task_key_norm = str(task_key or "").strip()
    row = db.get(LLMTaskPreset, (project_id, task_key_norm))
    if row is None:
        raise AppError.not_found()
    db.delete(row)
    db.commit()
    return ok_payload(request_id=request_id, data={})
