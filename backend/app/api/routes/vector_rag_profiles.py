from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.deps import DbDep, UserIdDep
from app.core.errors import AppError, ok_payload
from app.core.secrets import SecretCryptoError, encrypt_secret, mask_api_key
from app.db.utils import new_id
from app.models.vector_rag_profile import VectorRagProfile
from app.schemas.vector_rag_profiles import VectorRagProfileCreate, VectorRagProfileOut, VectorRagProfileUpdate

router = APIRouter()


def _require_owned_profile(db: DbDep, *, profile_id: str, user_id: str) -> VectorRagProfile:
    row = db.get(VectorRagProfile, profile_id)
    if row is None or row.owner_user_id != user_id:
        raise AppError.not_found()
    return row


def _to_out(row: VectorRagProfile) -> dict:
    return VectorRagProfileOut(
        id=row.id,
        owner_user_id=row.owner_user_id,
        name=row.name,
        vector_embedding_provider=row.vector_embedding_provider,
        vector_embedding_base_url=row.vector_embedding_base_url,
        vector_embedding_model=row.vector_embedding_model,
        vector_embedding_expected_dimension=int(row.vector_embedding_expected_dimension or 1536),
        vector_embedding_has_api_key=bool(row.vector_embedding_api_key_ciphertext),
        vector_embedding_masked_api_key=row.vector_embedding_api_key_masked,
        vector_rerank_provider=row.vector_rerank_provider,
        vector_rerank_base_url=row.vector_rerank_base_url,
        vector_rerank_model=row.vector_rerank_model,
        vector_rerank_has_api_key=bool(row.vector_rerank_api_key_ciphertext),
        vector_rerank_masked_api_key=row.vector_rerank_api_key_masked,
        created_at=row.created_at,
        updated_at=row.updated_at,
    ).model_dump()


def _set_api_key(row: VectorRagProfile, field_prefix: str, raw_key: str | None) -> None:
    cipher_attr = f"{field_prefix}_api_key_ciphertext"
    masked_attr = f"{field_prefix}_api_key_masked"
    if raw_key is None:
        return
    key = raw_key.strip()
    if not key:
        setattr(row, cipher_attr, None)
        setattr(row, masked_attr, None)
        return
    try:
        setattr(row, cipher_attr, encrypt_secret(key))
    except SecretCryptoError:
        raise AppError(code="SECRET_CONFIG_ERROR", message="服务端未配置 SECRET_ENCRYPTION_KEY", status_code=500)
    setattr(row, masked_attr, mask_api_key(key))


@router.get("/vector_rag_profiles")
def list_profiles(request: Request, db: DbDep, user_id: UserIdDep) -> dict:
    request_id = request.state.request_id
    rows = (
        db.execute(
            select(VectorRagProfile)
            .where(VectorRagProfile.owner_user_id == user_id)
            .order_by(VectorRagProfile.updated_at.desc())
        )
        .scalars()
        .all()
    )
    return ok_payload(request_id=request_id, data={"profiles": [_to_out(r) for r in rows]})


@router.post("/vector_rag_profiles")
def create_profile(request: Request, db: DbDep, user_id: UserIdDep, body: VectorRagProfileCreate) -> dict:
    request_id = request.state.request_id
    row = VectorRagProfile(
        id=new_id(),
        owner_user_id=user_id,
        name=body.name,
        vector_embedding_provider=(body.vector_embedding_provider or "").strip() or None,
        vector_embedding_base_url=(body.vector_embedding_base_url or "").strip() or None,
        vector_embedding_model=(body.vector_embedding_model or "").strip() or None,
        vector_embedding_expected_dimension=int(body.vector_embedding_expected_dimension),
        vector_rerank_provider=(body.vector_rerank_provider or "").strip() or None,
        vector_rerank_base_url=(body.vector_rerank_base_url or "").strip() or None,
        vector_rerank_model=(body.vector_rerank_model or "").strip() or None,
    )
    _set_api_key(row, "vector_embedding", body.vector_embedding_api_key)
    _set_api_key(row, "vector_rerank", body.vector_rerank_api_key)
    db.add(row)
    db.commit()
    db.refresh(row)
    return ok_payload(request_id=request_id, data={"profile": _to_out(row)})


@router.put("/vector_rag_profiles/{profile_id}")
def update_profile(
    request: Request, db: DbDep, user_id: UserIdDep, profile_id: str, body: VectorRagProfileUpdate
) -> dict:
    request_id = request.state.request_id
    row = _require_owned_profile(db, profile_id=profile_id, user_id=user_id)

    if body.name is not None:
        row.name = body.name

    if "vector_embedding_provider" in body.model_fields_set:
        row.vector_embedding_provider = (body.vector_embedding_provider or "").strip() or None
    if "vector_embedding_base_url" in body.model_fields_set:
        row.vector_embedding_base_url = (body.vector_embedding_base_url or "").strip() or None
    if "vector_embedding_model" in body.model_fields_set:
        row.vector_embedding_model = (body.vector_embedding_model or "").strip() or None
    if "vector_embedding_expected_dimension" in body.model_fields_set:
        row.vector_embedding_expected_dimension = int(body.vector_embedding_expected_dimension or 1536)
    if "vector_embedding_api_key" in body.model_fields_set:
        _set_api_key(row, "vector_embedding", body.vector_embedding_api_key)

    if "vector_rerank_provider" in body.model_fields_set:
        row.vector_rerank_provider = (body.vector_rerank_provider or "").strip() or None
    if "vector_rerank_base_url" in body.model_fields_set:
        row.vector_rerank_base_url = (body.vector_rerank_base_url or "").strip() or None
    if "vector_rerank_model" in body.model_fields_set:
        row.vector_rerank_model = (body.vector_rerank_model or "").strip() or None
    if "vector_rerank_api_key" in body.model_fields_set:
        _set_api_key(row, "vector_rerank", body.vector_rerank_api_key)

    db.commit()
    db.refresh(row)
    return ok_payload(request_id=request_id, data={"profile": _to_out(row)})


@router.delete("/vector_rag_profiles/{profile_id}")
def delete_profile(request: Request, db: DbDep, user_id: UserIdDep, profile_id: str) -> dict:
    request_id = request.state.request_id
    row = _require_owned_profile(db, profile_id=profile_id, user_id=user_id)
    db.delete(row)
    db.commit()
    return ok_payload(request_id=request_id, data={})
