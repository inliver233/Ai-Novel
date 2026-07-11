from __future__ import annotations

from datetime import timedelta, timezone

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.auth_session import build_session, clear_session_cookies, set_session_cookies
from app.core.config import settings
from app.core.errors import AppError, ok_payload
from app.db.utils import utc_now
from app.models.user import User


def user_public(user: User) -> dict:
    return {"id": user.id, "display_name": user.display_name, "is_admin": bool(user.is_admin)}


def get_current_user(request: Request, db: Session, user_id: str) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise AppError.unauthorized()
    expires_at = getattr(request.state, "session_expire_at", None)
    session_payload = (
        None if expires_at is None else {"expire_at": int(expires_at.astimezone(timezone.utc).timestamp())}
    )
    return ok_payload(request_id=request.state.request_id, data={"user": user_public(user), "session": session_payload})


def refresh_session(request: Request, user_id: str) -> JSONResponse:
    expires_at = getattr(request.state, "session_expire_at", None)
    if expires_at is None:
        raise AppError.unauthorized()
    now = utc_now()
    remaining_seconds = int((expires_at - now).total_seconds())
    refreshed = remaining_seconds <= settings.auth_refresh_threshold_seconds
    out_expires_at = now + timedelta(seconds=settings.auth_session_ttl_seconds) if refreshed else expires_at
    response = JSONResponse(
        ok_payload(
            request_id=request.state.request_id,
            data={
                "refreshed": refreshed,
                "session": {"expire_at": int(out_expires_at.astimezone(timezone.utc).timestamp())},
            },
        )
    )
    if refreshed:
        set_session_cookies(
            response,
            user_id=user_id,
            expires_at=out_expires_at,
            session_version=int(getattr(request.state, "session_version", 0) or 0),
        )
    return response


def logout(request: Request) -> JSONResponse:
    response = JSONResponse(ok_payload(request_id=request.state.request_id, data={}))
    clear_session_cookies(response)
    return response


def login_response(request: Request, user: User) -> JSONResponse:
    session = build_session(user_id=user.id, session_version=int(user.session_version or 0))
    response = JSONResponse(
        ok_payload(
            request_id=request.state.request_id,
            data={
                "user": user_public(user),
                "session": {"expire_at": int(session.expires_at.astimezone(timezone.utc).timestamp())},
            },
        )
    )
    set_session_cookies(
        response,
        user_id=user.id,
        expires_at=session.expires_at,
        issued_at=session.issued_at,
        session_version=session.session_version,
    )
    return response
