from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.api.deps import DbDep
from app.services.authentication import linuxdo

router = APIRouter()


@router.get("/auth/oidc/linuxdo/start", name="linuxdo_oidc_start")
def linuxdo_oidc_start(request: Request, next: str | None = None) -> RedirectResponse:
    return linuxdo.start(request, next)


@router.get("/auth/oidc/linuxdo/callback", name="linuxdo_oidc_callback")
def linuxdo_oidc_callback(
    request: Request, db: DbDep, code: str | None = None, state: str | None = None
) -> RedirectResponse:
    return linuxdo.callback(request, db, code, state)
