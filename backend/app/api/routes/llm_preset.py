from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import DbDep, UserIdDep, require_project_editor, require_project_viewer
from app.core.errors import ok_payload
from app.models.llm_preset import LLMPreset
from app.schemas.llm_preset import LLMPresetOut, LLMPresetPutRequest
from app.services.llm_contract_service import capability_contract, normalize_base_url_for_provider, normalize_max_tokens_for_provider, normalize_provider_model
from app.services.llm_profile_template import decode_extra_json, decode_stop_json, encode_extra_json, encode_stop_json

router = APIRouter()


def _default_preset(project_id: str) -> LLMPreset:
    provider, model = normalize_provider_model("openai", "gpt-4o-mini")
    return LLMPreset(
        project_id=project_id,
        provider=provider,
        base_url=normalize_base_url_for_provider(provider, None),
        model=model,
        temperature=0.7,
        top_p=1.0,
        max_tokens=normalize_max_tokens_for_provider(provider, model, None),
        presence_penalty=0.0,
        frequency_penalty=0.0,
        top_k=None,
        stop_json="[]",
        timeout_seconds=1200,
        extra_json="{}",
    )


def _to_out(row: LLMPreset) -> dict:
    meta = capability_contract(str(row.provider or "").strip(), str(row.model or "").strip())
    return LLMPresetOut(
        project_id=row.project_id,
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
    ).model_dump()


@router.get("/projects/{project_id}/llm_preset")
def get_llm_preset(request: Request, db: DbDep, user_id: UserIdDep, project_id: str) -> dict:
    request_id = request.state.request_id
    require_project_viewer(db, project_id=project_id, user_id=user_id)
    row = db.get(LLMPreset, project_id)
    if row is None:
        row = _default_preset(project_id)
    return ok_payload(request_id=request_id, data={"llm_preset": _to_out(row)})


@router.put("/projects/{project_id}/llm_preset")
def put_llm_preset(
    request: Request,
    db: DbDep,
    user_id: UserIdDep,
    project_id: str,
    body: LLMPresetPutRequest,
) -> dict:
    request_id = request.state.request_id
    require_project_editor(db, project_id=project_id, user_id=user_id)

    provider, model = normalize_provider_model(body.provider, body.model)
    base_url = normalize_base_url_for_provider(provider, body.base_url)

    row = db.get(LLMPreset, project_id)
    if row is None:
        row = _default_preset(project_id)
        db.add(row)

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
    return ok_payload(request_id=request_id, data={"llm_preset": _to_out(row)})
