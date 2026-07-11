"""Compatibility facade for callers that still include the historical auth router."""

from fastapi import APIRouter

from app.api.routes import auth_admin, auth_local, auth_oidc_linuxdo, auth_session
from app.schemas.auth import (
    AdminCreateUserRequest,
    AdminResetPasswordRequest,
    ChangePasswordRequest,
    DisableUserRequest,
    LocalLoginRequest,
    LocalRegisterRequest,
)

router = APIRouter()
router.include_router(auth_session.router)
router.include_router(auth_local.router)
router.include_router(auth_oidc_linuxdo.router)
router.include_router(auth_admin.router)

__all__ = [
    "AdminCreateUserRequest",
    "AdminResetPasswordRequest",
    "ChangePasswordRequest",
    "DisableUserRequest",
    "LocalLoginRequest",
    "LocalRegisterRequest",
    "router",
]
