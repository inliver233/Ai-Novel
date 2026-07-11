from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.deps import AuthenticatedUserIdDep, DbDep
from app.core.errors import ok_payload
from app.services.authentication import linuxdo
from app.services.authentication import session as auth_session

router = APIRouter()


@router.get("/auth/user")
def get_current_user(request: Request, db: DbDep, user_id: AuthenticatedUserIdDep) -> dict:
    return auth_session.get_current_user(request, db, user_id)


@router.get("/auth/providers")
def list_auth_providers(request: Request) -> dict:
    return ok_payload(
        request_id=request.state.request_id,
        data={"local": {"enabled": True}, "linuxdo": {"enabled": linuxdo.enabled()}},
    )


@router.post("/auth/refresh")
def refresh_session(request: Request, user_id: AuthenticatedUserIdDep) -> JSONResponse:
    return auth_session.refresh_session(request, user_id)


@router.post("/auth/logout")
def logout(request: Request) -> JSONResponse:
    return auth_session.logout(request)
