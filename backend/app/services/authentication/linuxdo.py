from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth_session import build_session, clear_session_cookies, set_session_cookies
from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import log_event
from app.db.utils import new_id
from app.models.auth_external_account import AuthExternalAccount
from app.models.user import User
from app.services.authentication import oidc_client

logger = logging.getLogger("ainovel")
_PROVIDER = "linuxdo"
_STATE_COOKIE = "oidc_linuxdo_state"
_VERIFIER_COOKIE = "oidc_linuxdo_verifier"
_NEXT_COOKIE = "oidc_linuxdo_next"
_COOKIE_MAX_AGE_SECONDS = 10 * 60
_USER_ID_SANITIZE_RE = re.compile(r"[^a-z0-9_-]+")


def enabled() -> bool:
    return bool((settings.linuxdo_oidc_client_id or "").strip() and (settings.linuxdo_oidc_client_secret or "").strip())


def safe_next_path(value: str | None) -> str:
    raw = str(value or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        raw = raw[1:-1].strip()
    return raw if raw and raw.startswith("/") and not raw.startswith("//") else "/"


def _pkce_code_verifier() -> str:
    verifier = secrets.token_urlsafe(96)
    if len(verifier) < 43:
        verifier = (verifier + secrets.token_urlsafe(96))[:96]
    return verifier[:128]


def _pkce_code_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest()).rstrip(b"=").decode("ascii")


def _cookie_kwargs() -> dict[str, object]:
    return {
        "httponly": True,
        "secure": settings.app_env == "prod",
        "samesite": "lax",
        "max_age": _COOKIE_MAX_AGE_SECONDS,
        "path": "/",
    }


def suggest_user_id(db: Session, *, login: str) -> str:
    login_norm = _USER_ID_SANITIZE_RE.sub("_", str(login or "").strip().lower()).strip("_") or new_id().split("-", 1)[0]
    base = f"linuxdo_{login_norm[:48]}".strip("_")[:64] or f"linuxdo_{new_id().split('-', 1)[0]}"
    if db.get(User, base) is None:
        return base
    for _ in range(8):
        suffix = secrets.token_urlsafe(4).replace("-", "").replace("_", "")[:6].lower()
        candidate = f"{base[: (64 - 1 - len(suffix))]}_{suffix}"
        if db.get(User, candidate) is None:
            return candidate
    return f"{base[: (64 - 1 - 8)]}_{new_id().split('-', 1)[0][:8]}"


def start(request: Request, next_path_raw: str | None = None) -> RedirectResponse:
    if not enabled():
        raise AppError(
            code="OIDC_NOT_CONFIGURED", message="LinuxDo OIDC 未配置（缺少 client_id/client_secret）", status_code=400
        )
    discovery = oidc_client.get_linuxdo_discovery(
        discovery_url=settings.linuxdo_oidc_discovery_url, ttl_seconds=settings.linuxdo_oidc_discovery_ttl_seconds
    )
    state, verifier = secrets.token_urlsafe(24), _pkce_code_verifier()
    redirect_uri = (settings.linuxdo_oidc_redirect_uri or "").strip() or str(request.url_for("linuxdo_oidc_callback"))
    params = {
        "response_type": "code",
        "client_id": str(settings.linuxdo_oidc_client_id or "").strip(),
        "redirect_uri": redirect_uri,
        "scope": str(settings.linuxdo_oidc_scopes or "openid profile email").strip() or "openid profile email",
        "state": state,
        "code_challenge": _pkce_code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    response = RedirectResponse(url=f"{discovery['authorization_endpoint']}?{urlencode(params)}", status_code=302)
    response.set_cookie(_STATE_COOKIE, state, **_cookie_kwargs())
    response.set_cookie(_VERIFIER_COOKIE, verifier, **_cookie_kwargs())
    response.set_cookie(_NEXT_COOKIE, safe_next_path(next_path_raw), **_cookie_kwargs())
    return response


def callback(request: Request, db: Session, code: str | None = None, state: str | None = None) -> RedirectResponse:
    request_id = request.state.request_id
    next_path = safe_next_path(request.cookies.get(_NEXT_COOKIE))

    def clear(resp: RedirectResponse) -> None:
        for name in (_STATE_COOKIE, _VERIFIER_COOKIE, _NEXT_COOKIE):
            resp.delete_cookie(key=name, path="/", secure=settings.app_env == "prod", samesite="lax")

    def fail(error_code: str) -> RedirectResponse:
        resp = RedirectResponse(
            url="/login?" + urlencode({"next": next_path, "oidc_error": error_code, "request_id": request_id}),
            status_code=302,
        )
        clear(resp)
        if error_code == "ACCOUNT_DISABLED":
            clear_session_cookies(resp)
        return resp

    response = RedirectResponse(url=next_path, status_code=302)
    clear(response)
    if not enabled():
        return fail("OIDC_NOT_CONFIGURED")
    state_cookie, state_q = str(request.cookies.get(_STATE_COOKIE) or "").strip(), str(state or "").strip()
    if not state_cookie or not state_q or not secrets.compare_digest(state_cookie, state_q):
        return fail("OIDC_STATE_MISMATCH")
    code_q = str(code or "").strip()
    if not code_q:
        return fail("OIDC_CODE_MISSING")
    verifier = str(request.cookies.get(_VERIFIER_COOKIE) or "").strip()
    if not verifier:
        return fail("OIDC_VERIFIER_MISSING")
    try:
        discovery = oidc_client.get_linuxdo_discovery(
            discovery_url=settings.linuxdo_oidc_discovery_url, ttl_seconds=settings.linuxdo_oidc_discovery_ttl_seconds
        )
        redirect_uri = (settings.linuxdo_oidc_redirect_uri or "").strip() or str(
            request.url_for("linuxdo_oidc_callback")
        )
        token_res = oidc_client.exchange_linuxdo_code_for_token(
            token_endpoint=discovery["token_endpoint"],
            code=code_q,
            redirect_uri=redirect_uri,
            code_verifier=verifier,
            client_id=str(settings.linuxdo_oidc_client_id or "").strip(),
            client_secret=str(settings.linuxdo_oidc_client_secret or "").strip(),
        )
        access_token = str(token_res.get("access_token") or "").strip()
        if not access_token:
            return fail("OIDC_TOKEN_MISSING")
        userinfo = oidc_client.fetch_linuxdo_userinfo(
            userinfo_endpoint=discovery["userinfo_endpoint"], access_token=access_token
        )
        subject = str(userinfo.get("sub") or "").strip()
        if not subject:
            return fail("OIDC_SUBJECT_MISSING")
    except AppError as exc:
        log_event(
            logger,
            "warning",
            event="AUTH_OIDC",
            action="callback_failed",
            provider=_PROVIDER,
            error_code=exc.code,
            exception_type=type(exc).__name__,
        )
        return fail(exc.code)
    except Exception as exc:
        log_event(
            logger,
            "error",
            event="AUTH_OIDC",
            action="callback_failed",
            provider=_PROVIDER,
            error_code="OIDC_UNKNOWN",
            exception_type=type(exc).__name__,
        )
        return fail("OIDC_UNKNOWN")
    login = str(userinfo.get("login") or userinfo.get("username") or "").strip()
    display_name = str(userinfo.get("name") or login or "LinuxDo 用户").strip() or "LinuxDo 用户"
    email_raw = str(userinfo.get("email") or "").strip() or None
    avatar_url = str(userinfo.get("avatar_url") or "").strip() or None
    user: User | None = None
    for attempt in range(3):
        ext = db.get(AuthExternalAccount, (_PROVIDER, subject))
        user = db.get(User, str(ext.user_id)) if ext is not None else None
        email = email_raw if attempt == 0 else None
        if email:
            existing = db.execute(select(User.id).where(User.email == email).limit(1)).scalars().first()
            if existing and (user is None or str(existing) != str(getattr(user, "id", ""))):
                email = None
        user_created = False
        if user is None:
            user_id = (
                str(ext.user_id)
                if ext is not None
                else (
                    suggest_user_id(db, login=login or display_name)
                    if attempt == 0
                    else f"linuxdo_{new_id().split('-', 1)[0]}"
                )
            )
            user = User(id=user_id, email=email, display_name=display_name, is_admin=False)
            db.add(user)
            user_created = True
        else:
            if email and not user.email:
                user.email = email
            if display_name and not user.display_name:
                user.display_name = display_name
        try:
            if user_created:
                db.flush([user])
            if ext is None:
                db.add(
                    AuthExternalAccount(
                        provider=_PROVIDER,
                        subject=subject,
                        user_id=str(user.id),
                        username=login or None,
                        email=email_raw,
                        avatar_url=avatar_url,
                    )
                )
            else:
                ext.username, ext.email, ext.avatar_url = (
                    login or ext.username,
                    email_raw or ext.email,
                    avatar_url or ext.avatar_url,
                )
            db.commit()
            break
        except IntegrityError as exc:
            db.rollback()
            try:
                db.expunge_all()
            except Exception:
                pass
            ext = db.get(AuthExternalAccount, (_PROVIDER, subject))
            if ext is not None:
                user = db.get(User, str(ext.user_id))
                if user is not None:
                    break
            if attempt >= 2:
                log_event(
                    logger,
                    "warning",
                    event="AUTH_OIDC",
                    action="db_conflict",
                    provider=_PROVIDER,
                    error_code="OIDC_DB_CONFLICT",
                    pgcode=str(getattr(getattr(exc, "orig", None), "pgcode", "") or ""),
                    constraint_name=str(
                        getattr(getattr(getattr(exc, "orig", None), "diag", None), "constraint_name", "") or ""
                    ),
                    exception_type=type(exc).__name__,
                )
                return fail("OIDC_DB_CONFLICT")
    if user is None:
        log_event(
            logger,
            "warning",
            event="AUTH_OIDC",
            action="db_missing_user",
            provider=_PROVIDER,
            error_code="OIDC_DB_ERROR",
        )
        return fail("OIDC_DB_ERROR")
    if user.disabled_at is not None:
        return fail("ACCOUNT_DISABLED")
    session = build_session(user_id=user.id, session_version=int(user.session_version or 0))
    set_session_cookies(
        response,
        user_id=user.id,
        expires_at=session.expires_at,
        issued_at=session.issued_at,
        session_version=session.session_version,
    )
    return response
