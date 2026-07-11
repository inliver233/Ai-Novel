from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.api.deps import AdminUserDep, DbDep
from app.schemas.auth import AdminCreateUserRequest, AdminResetPasswordRequest, DisableUserRequest
from app.services.authentication import admin

router = APIRouter()


@router.post("/auth/admin/users/{target_user_id}/disable")
def set_user_disabled(
    request: Request, db: DbDep, _admin_user: AdminUserDep, target_user_id: str, body: DisableUserRequest
) -> dict:
    return admin.set_user_disabled(request, db, target_user_id, body)


@router.get("/auth/admin/users")
def list_users(
    request: Request,
    db: DbDep,
    _admin_user: AdminUserDep,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=64),
    q: str | None = Query(default=None, max_length=128),
    online_only: bool = Query(default=False),
) -> dict:
    return admin.list_users(request, db, limit=limit, cursor=cursor, q=q, online_only=online_only)


@router.post("/auth/admin/users")
def create_user(request: Request, db: DbDep, _admin_user: AdminUserDep, body: AdminCreateUserRequest) -> dict:
    return admin.create_user(request, db, body)


@router.post("/auth/admin/users/{target_user_id}/password/reset")
def reset_user_password(
    request: Request, db: DbDep, _admin_user: AdminUserDep, target_user_id: str, body: AdminResetPasswordRequest
) -> dict:
    return admin.reset_user_password(request, db, target_user_id, body)
