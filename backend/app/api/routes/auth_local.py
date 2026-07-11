from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.deps import AuthenticatedUserIdDep, DbDep
from app.schemas.auth import ChangePasswordRequest, LocalLoginRequest, LocalRegisterRequest
from app.services.authentication import local

router = APIRouter()


@router.post("/auth/local/login")
def local_login(request: Request, db: DbDep, body: LocalLoginRequest) -> JSONResponse:
    return local.local_login(request, db, body)


@router.post("/auth/local/register")
def local_register(request: Request, db: DbDep, body: LocalRegisterRequest) -> JSONResponse:
    return local.local_register(request, db, body)


@router.post("/auth/password/change")
def change_password(request: Request, db: DbDep, user_id: AuthenticatedUserIdDep, body: ChangePasswordRequest) -> dict:
    return local.change_password(request, db, user_id, body)
