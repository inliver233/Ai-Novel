from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import AuthenticatedUserIdDep, DbDep
from app.core.auth_session import build_session, clear_session_cookies, set_session_cookies
from app.core.config import settings
from app.core.errors import AppError, ok_payload
from app.core.logging import log_event
from app.db.datetime_compat import coerce_utc_datetime
from app.db.utils import new_id, utc_now
from app.models.auth_external_account import AuthExternalAccount
from app.models.user import User
from app.models.user_activity_stat import UserActivityStat
from app.models.user_password import UserPassword
from app.models.user_usage_stat import UserUsageStat
from app.schemas.base import RequestModel
from app.services.auth_service import commit_user_creation, hash_password, verify_password

router = APIRouter()
logger = logging.getLogger("ainovel")

_LINUXDO_PROVIDER = "linuxdo"
_LINUXDO_OIDC_STATE_COOKIE = "oidc_linuxdo_state"
_LINUXDO_OIDC_VERIFIER_COOKIE = "oidc_linuxdo_verifier"
_LINUXDO_OIDC_NEXT_COOKIE = "oidc_linuxdo_next"
_LINUXDO_OIDC_COOKIE_MAX_AGE_SECONDS = 10 * 60

_USER_ID_SANITIZE_RE = re.compile(r"[^a-z0-9_-]+")


def _to_utc_epoch(value: datetime | None) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).timestamp()
    return value.astimezone(timezone.utc).timestamp()


def _linuxdo_oidc_enabled() -> bool:
    return bool((settings.linuxdo_oidc_client_id or "").strip() and (settings.linuxdo_oidc_client_secret or "").strip())


def _safe_next_path(value: str | None) -> str:
    raw = str(value or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        raw = raw[1:-1].strip()
    if not raw:
        return "/"
    if not raw.startswith("/"):
        return "/"
    if raw.startswith("//"):
        return "/"
    return raw


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _pkce_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return _b64url(digest)


def _pkce_code_verifier() -> str:
    verifier = secrets.token_urlsafe(96)
    if len(verifier) < 43:
        verifier = (verifier + secrets.token_urlsafe(96))[:96]
    return verifier[:128]


def _linuxdo_discovery() -> dict[str, str]:
    try:
        import httpx

        url = str(settings.linuxdo_oidc_discovery_url or "").strip()
        if not url:
            raise ValueError("missing_discovery_url")
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        raise AppError(
            code="OIDC_DISCOVERY_FAILED",
            message="LinuxDo OIDC discovery 获取失败",
            status_code=502,
            details={"provider": _LINUXDO_PROVIDER, "error_type": type(exc).__name__},
        ) from exc

    if not isinstance(data, dict):
        raise AppError(code="OIDC_DISCOVERY_FAILED", message="LinuxDo OIDC discovery 响应无效", status_code=502, details={"provider": _LINUXDO_PROVIDER})

    def _req(key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise AppError(
                code="OIDC_DISCOVERY_FAILED",
                message=f"LinuxDo OIDC discovery 缺少字段：{key}",
                status_code=502,
                details={"provider": _LINUXDO_PROVIDER, "missing_key": key},
            )
        return value.strip()

    return {
        "authorization_endpoint": _req("authorization_endpoint"),
        "token_endpoint": _req("token_endpoint"),
        "userinfo_endpoint": _req("userinfo_endpoint"),
        "issuer": _req("issuer"),
    }


def _linuxdo_exchange_code_for_token(*, token_endpoint: str, code: str, redirect_uri: str, code_verifier: str) -> dict:
    try:
        import httpx

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": str(settings.linuxdo_oidc_client_id or "").strip(),
            "client_secret": str(settings.linuxdo_oidc_client_secret or "").strip(),
            "code_verifier": code_verifier,
        }
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(token_endpoint, data=payload, headers={"Accept": "application/json"})
            resp.raise_for_status()
            out = resp.json()
    except Exception as exc:
        raise AppError(
            code="OIDC_TOKEN_EXCHANGE_FAILED",
            message="LinuxDo OIDC token 交换失败",
            status_code=502,
            details={"provider": _LINUXDO_PROVIDER, "error_type": type(exc).__name__},
        ) from exc

    return out if isinstance(out, dict) else {}


def _linuxdo_fetch_userinfo(*, userinfo_endpoint: str, access_token: str) -> dict:
    try:
        import httpx

        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                userinfo_endpoint,
                headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            out = resp.json()
    except Exception as exc:
        raise AppError(
            code="OIDC_USERINFO_FAILED",
            message="LinuxDo OIDC userinfo 获取失败",
            status_code=502,
            details={"provider": _LINUXDO_PROVIDER, "error_type": type(exc).__name__},
        ) from exc

    return out if isinstance(out, dict) else {}


def _oidc_cookie_kwargs() -> dict[str, object]:
    return {
        "httponly": True,
        "secure": settings.app_env == "prod",
        # Must be lax for cross-site OIDC redirects.
        "samesite": "lax",
        "max_age": _LINUXDO_OIDC_COOKIE_MAX_AGE_SECONDS,
        "path": "/",
    }


def _linuxdo_suggest_user_id(db: DbDep, *, login: str) -> str:
    login_norm = _USER_ID_SANITIZE_RE.sub("_", str(login or "").strip().lower()).strip("_")
    if not login_norm:
        login_norm = new_id().split("-", 1)[0]

    base = f"linuxdo_{login_norm[:48]}".strip("_")[:64]
    if not base:
        base = f"linuxdo_{new_id().split('-', 1)[0]}"
    if db.get(User, base) is None:
        return base

    for _ in range(8):
        suffix = secrets.token_urlsafe(4).replace("-", "").replace("_", "")[:6].lower()
        candidate = f"{base[: (64 - 1 - len(suffix))]}_{suffix}"
        if db.get(User, candidate) is None:
            return candidate

    return f"{base[: (64 - 1 - 8)]}_{new_id().split('-', 1)[0][:8]}"


def _user_public(user: User) -> dict:
    return {"id": user.id, "display_name": user.display_name, "is_admin": bool(user.is_admin)}


def _require_admin(db: DbDep, *, user_id: str) -> User:
    actor = db.get(User, user_id)
    if actor is None or not actor.is_admin:
        raise AppError.forbidden()
    return actor


def _user_admin_public(
    *,
    user: User,
    pwd: UserPassword | None,
    activity: UserActivityStat | None = None,
    usage: UserUsageStat | None = None,
    online_cutoff=None,
) -> dict:
    last_seen_at = coerce_utc_datetime(getattr(activity, "last_seen_at", None))
    online = False
    if isinstance(last_seen_at, datetime) and isinstance(online_cutoff, datetime):
        last_seen_epoch = _to_utc_epoch(last_seen_at)
        cutoff_epoch = _to_utc_epoch(online_cutoff)
        online = bool(last_seen_epoch is not None and cutoff_epoch is not None and last_seen_epoch >= cutoff_epoch)
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "is_admin": bool(user.is_admin),
        "disabled": bool(getattr(pwd, "disabled_at", None) is not None),
        "password_updated_at": getattr(pwd, "password_updated_at", None),
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "activity": {
            "online": online,
            "last_seen_at": last_seen_at,
            "last_seen_request_id": getattr(activity, "last_seen_request_id", None),
            "last_seen_path": getattr(activity, "last_seen_path", None),
            "last_seen_method": getattr(activity, "last_seen_method", None),
            "last_seen_status": getattr(activity, "last_seen_status", None),
        },
        "usage": {
            "total_generation_calls": int(getattr(usage, "total_generation_calls", 0) or 0),
            "total_generation_error_calls": int(getattr(usage, "total_generation_error_calls", 0) or 0),
            "total_generated_chars": int(getattr(usage, "total_generated_chars", 0) or 0),
            "last_generation_at": getattr(usage, "last_generation_at", None),
        },
    }

class LocalLoginRequest(RequestModel):
    user_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class LocalRegisterRequest(RequestModel):
    user_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    display_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)


class ChangePasswordRequest(RequestModel):
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class DisableUserRequest(RequestModel):
    disabled: bool = True


class AdminCreateUserRequest(RequestModel):
    user_id: str = Field(min_length=1, max_length=64)
    display_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    is_admin: bool = False
    password: str | None = Field(default=None, max_length=256)


class AdminResetPasswordRequest(RequestModel):
    new_password: str | None = Field(default=None, max_length=256)


@router.get("/auth/user")
def get_current_user(request: Request, db: DbDep, user_id: AuthenticatedUserIdDep) -> dict:
    request_id = request.state.request_id
    user = db.get(User, user_id)
    if user is None:
        raise AppError.unauthorized()

    expires_at = getattr(request.state, "session_expire_at", None)
    session_payload = None
    if expires_at is not None:
        session_payload = {"expire_at": int(expires_at.astimezone(timezone.utc).timestamp())}

    return ok_payload(request_id=request_id, data={"user": _user_public(user), "session": session_payload})


@router.get("/auth/providers")
def list_auth_providers(request: Request) -> dict:
    request_id = request.state.request_id
    return ok_payload(
        request_id=request_id,
        data={
            "local": {"enabled": True},
            "linuxdo": {"enabled": _linuxdo_oidc_enabled()},
        },
    )


@router.post("/auth/local/login")
def local_login(request: Request, db: DbDep, body: LocalLoginRequest) -> JSONResponse:
    request_id = request.state.request_id
    user = db.get(User, body.user_id)
    pwd = db.get(UserPassword, body.user_id)
    if user is None or pwd is None:
        raise AppError.unauthorized("用户名或密码错误")
    if pwd.disabled_at is not None:
        raise AppError.unauthorized("账号已禁用")
    if not verify_password(body.password, pwd.password_hash):
        raise AppError.unauthorized("用户名或密码错误")

    session = build_session(user_id=user.id)
    response = JSONResponse(
        ok_payload(
            request_id=request_id,
            data={
                "user": _user_public(user),
                "session": {"expire_at": int(session.expires_at.astimezone(timezone.utc).timestamp())},
            },
        )
    )
    set_session_cookies(response, user_id=user.id, expires_at=session.expires_at)
    return response


@router.post("/auth/local/register")
def local_register(request: Request, db: DbDep, body: LocalRegisterRequest) -> JSONResponse:
    request_id = request.state.request_id

    target_user_id = body.user_id.strip()
    if not target_user_id:
        raise AppError.validation("user_id 不能为空")

    admin_user_id = (settings.auth_admin_user_id or "").strip()
    if admin_user_id and target_user_id == admin_user_id:
        raise AppError.forbidden("该用户名已被系统保留，请联系管理员分配/重置")

    if db.get(User, target_user_id) is not None:
        raise AppError.conflict("用户已存在")

    email = (body.email or "").strip() or None
    display_name = (body.display_name or "").strip() or target_user_id
    user = User(id=target_user_id, email=email, display_name=display_name, is_admin=False)
    db.add(user)

    pwd = UserPassword(
        user_id=target_user_id,
        password_hash=hash_password(body.password),
        password_updated_at=utc_now(),
        disabled_at=None,
    )
    db.add(pwd)
    commit_user_creation(db)

    session = build_session(user_id=user.id)
    response = JSONResponse(
        ok_payload(
            request_id=request_id,
            data={
                "user": _user_public(user),
                "session": {"expire_at": int(session.expires_at.astimezone(timezone.utc).timestamp())},
            },
        )
    )
    set_session_cookies(response, user_id=user.id, expires_at=session.expires_at)
    return response


@router.get("/auth/oidc/linuxdo/start", name="linuxdo_oidc_start")
def linuxdo_oidc_start(request: Request, next: str | None = None) -> RedirectResponse:
    if not _linuxdo_oidc_enabled():
        raise AppError(code="OIDC_NOT_CONFIGURED", message="LinuxDo OIDC 未配置（缺少 client_id/client_secret）", status_code=400)

    discovery = _linuxdo_discovery()

    state = secrets.token_urlsafe(24)
    verifier = _pkce_code_verifier()
    challenge = _pkce_code_challenge(verifier)

    redirect_uri = (settings.linuxdo_oidc_redirect_uri or "").strip() or str(request.url_for("linuxdo_oidc_callback"))
    next_path = _safe_next_path(next)

    params = {
        "response_type": "code",
        "client_id": str(settings.linuxdo_oidc_client_id or "").strip(),
        "redirect_uri": redirect_uri,
        "scope": str(settings.linuxdo_oidc_scopes or "openid profile email").strip() or "openid profile email",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{discovery['authorization_endpoint']}?{urlencode(params)}"

    response = RedirectResponse(url=url, status_code=302)
    response.set_cookie(_LINUXDO_OIDC_STATE_COOKIE, state, **_oidc_cookie_kwargs())
    response.set_cookie(_LINUXDO_OIDC_VERIFIER_COOKIE, verifier, **_oidc_cookie_kwargs())
    response.set_cookie(_LINUXDO_OIDC_NEXT_COOKIE, next_path, **_oidc_cookie_kwargs())
    return response


@router.get("/auth/oidc/linuxdo/callback", name="linuxdo_oidc_callback")
def linuxdo_oidc_callback(request: Request, db: DbDep, code: str | None = None, state: str | None = None) -> RedirectResponse:
    request_id = request.state.request_id
    next_path = _safe_next_path(request.cookies.get(_LINUXDO_OIDC_NEXT_COOKIE))

    def _clear_oidc_cookies(resp: RedirectResponse) -> None:
        for name in (_LINUXDO_OIDC_STATE_COOKIE, _LINUXDO_OIDC_VERIFIER_COOKIE, _LINUXDO_OIDC_NEXT_COOKIE):
            resp.delete_cookie(key=name, path="/", secure=settings.app_env == "prod", samesite="lax")

    def _fail(error_code: str) -> RedirectResponse:
        url = "/login?" + urlencode({"next": next_path, "oidc_error": error_code, "request_id": request_id})
        resp = RedirectResponse(url=url, status_code=302)
        _clear_oidc_cookies(resp)
        return resp

    response = RedirectResponse(url=next_path, status_code=302)
    _clear_oidc_cookies(response)

    if not _linuxdo_oidc_enabled():
        return _fail("OIDC_NOT_CONFIGURED")

    state_cookie = str(request.cookies.get(_LINUXDO_OIDC_STATE_COOKIE) or "").strip()
    state_q = str(state or "").strip()
    if not state_cookie or not state_q or not secrets.compare_digest(state_cookie, state_q):
        return _fail("OIDC_STATE_MISMATCH")

    code_q = str(code or "").strip()
    if not code_q:
        return _fail("OIDC_CODE_MISSING")

    verifier = str(request.cookies.get(_LINUXDO_OIDC_VERIFIER_COOKIE) or "").strip()
    if not verifier:
        return _fail("OIDC_VERIFIER_MISSING")

    try:
        discovery = _linuxdo_discovery()
        redirect_uri = (settings.linuxdo_oidc_redirect_uri or "").strip() or str(request.url_for("linuxdo_oidc_callback"))
        token_res = _linuxdo_exchange_code_for_token(
            token_endpoint=discovery["token_endpoint"],
            code=code_q,
            redirect_uri=redirect_uri,
            code_verifier=verifier,
        )
        access_token = str(token_res.get("access_token") or "").strip()
        if not access_token:
            return _fail("OIDC_TOKEN_MISSING")

        userinfo = _linuxdo_fetch_userinfo(userinfo_endpoint=discovery["userinfo_endpoint"], access_token=access_token)
        subject = str(userinfo.get("sub") or "").strip()
        if not subject:
            return _fail("OIDC_SUBJECT_MISSING")
    except AppError as exc:
        log_event(
            logger,
            "warning",
            event="AUTH_OIDC",
            action="callback_failed",
            provider=_LINUXDO_PROVIDER,
            error_code=exc.code,
            exception_type=type(exc).__name__,
        )
        return _fail(exc.code)
    except Exception as exc:
        log_event(
            logger,
            "error",
            event="AUTH_OIDC",
            action="callback_failed",
            provider=_LINUXDO_PROVIDER,
            error_code="OIDC_UNKNOWN",
            exception_type=type(exc).__name__,
        )
        return _fail("OIDC_UNKNOWN")

    login = str(userinfo.get("login") or userinfo.get("username") or "").strip()
    display_name = str(userinfo.get("name") or login or "LinuxDo 用户").strip() or "LinuxDo 用户"
    email_raw = str(userinfo.get("email") or "").strip() or None
    avatar_url = str(userinfo.get("avatar_url") or "").strip() or None

    user: User | None = None

    for attempt in range(3):
        ext = db.get(AuthExternalAccount, (_LINUXDO_PROVIDER, subject))
        user = None
        if ext is not None:
            user = db.get(User, str(ext.user_id))

        email = email_raw if attempt == 0 else None
        if email:
            existing_email_user = db.execute(select(User.id).where(User.email == email).limit(1)).scalars().first()
            if existing_email_user and (user is None or str(existing_email_user) != str(getattr(user, "id", ""))):
                email = None

        user_created = False
        if user is None:
            if ext is not None:
                user_id = str(ext.user_id)
            else:
                user_id = _linuxdo_suggest_user_id(db, login=login or display_name) if attempt == 0 else f"linuxdo_{new_id().split('-', 1)[0]}"
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
                # Ensure the user row exists before inserting the external account mapping.
                db.flush([user])

            if ext is None:
                ext = AuthExternalAccount(
                    provider=_LINUXDO_PROVIDER,
                    subject=subject,
                    user_id=str(user.id),
                    username=login or None,
                    email=email_raw,
                    avatar_url=avatar_url,
                )
                db.add(ext)
            else:
                ext.username = login or ext.username
                ext.email = email_raw or ext.email
                ext.avatar_url = avatar_url or ext.avatar_url

            db.commit()
            break
        except IntegrityError as exc:
            db.rollback()
            try:
                db.expunge_all()
            except Exception:
                pass

            ext = db.get(AuthExternalAccount, (_LINUXDO_PROVIDER, subject))
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
                    provider=_LINUXDO_PROVIDER,
                    error_code="OIDC_DB_CONFLICT",
                    pgcode=str(getattr(getattr(exc, "orig", None), "pgcode", "") or ""),
                    constraint_name=str(getattr(getattr(getattr(exc, "orig", None), "diag", None), "constraint_name", "") or ""),
                    exception_type=type(exc).__name__,
                )
                return _fail("OIDC_DB_CONFLICT")
            continue

    if user is None:
        log_event(
            logger,
            "warning",
            event="AUTH_OIDC",
            action="db_missing_user",
            provider=_LINUXDO_PROVIDER,
            error_code="OIDC_DB_ERROR",
        )
        return _fail("OIDC_DB_ERROR")

    session = build_session(user_id=user.id)
    set_session_cookies(response, user_id=user.id, expires_at=session.expires_at)
    return response


@router.post("/auth/password/change")
def change_password(request: Request, db: DbDep, user_id: AuthenticatedUserIdDep, body: ChangePasswordRequest) -> dict:
    request_id = request.state.request_id
    pwd = db.get(UserPassword, user_id)
    if pwd is None or pwd.disabled_at is not None:
        raise AppError.unauthorized()
    if not verify_password(body.old_password, pwd.password_hash):
        raise AppError.unauthorized("旧密码错误")

    pwd.password_hash = hash_password(body.new_password)
    pwd.password_updated_at = utc_now()
    db.commit()

    return ok_payload(request_id=request_id, data={})


@router.post("/auth/admin/users/{target_user_id}/disable")
def set_user_disabled(
    request: Request,
    db: DbDep,
    user_id: AuthenticatedUserIdDep,
    target_user_id: str,
    body: DisableUserRequest,
) -> dict:
    request_id = request.state.request_id
    _require_admin(db, user_id=user_id)

    pwd = db.get(UserPassword, target_user_id)
    if pwd is None:
        raise AppError.not_found()

    pwd.disabled_at = utc_now() if body.disabled else None
    db.commit()

    return ok_payload(request_id=request_id, data={})


@router.get("/auth/admin/users")
def list_users(
    request: Request,
    db: DbDep,
    user_id: AuthenticatedUserIdDep,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=64),
    q: str | None = Query(default=None, max_length=128),
    online_only: bool = Query(default=False),
) -> dict:
    request_id = request.state.request_id
    _require_admin(db, user_id=user_id)

    now = utc_now()
    online_cutoff = now - timedelta(seconds=int(settings.auth_online_window_seconds or 300))
    cursor_value = str(cursor or "").strip() or None
    q_value = str(q or "").strip().lower()
    search_pattern = f"%{q_value}%"

    filters = []
    if q_value:
        filters.append(
            or_(
                func.lower(User.id).like(search_pattern),
                func.lower(func.coalesce(User.display_name, "")).like(search_pattern),
                func.lower(func.coalesce(User.email, "")).like(search_pattern),
            )
        )
    if online_only:
        filters.append(UserActivityStat.last_seen_at.is_not(None))
        filters.append(UserActivityStat.last_seen_at >= online_cutoff)

    list_stmt = (
        select(User, UserPassword, UserActivityStat, UserUsageStat)
        .join(UserPassword, UserPassword.user_id == User.id, isouter=True)
        .join(UserActivityStat, UserActivityStat.user_id == User.id, isouter=True)
        .join(UserUsageStat, UserUsageStat.user_id == User.id, isouter=True)
    )
    if filters:
        list_stmt = list_stmt.where(*filters)
    if cursor_value:
        list_stmt = list_stmt.where(User.id > cursor_value)
    rows = db.execute(list_stmt.order_by(User.id.asc()).limit(limit + 1)).all()

    has_more = len(rows) > limit
    paged_rows = rows[:limit]
    users = [
        _user_admin_public(
            user=u,
            pwd=p,
            activity=a,
            usage=usg,
            online_cutoff=online_cutoff,
        )
        for u, p, a, usg in paged_rows
    ]

    next_cursor = None
    if has_more and users:
        next_cursor = str(users[-1].get("id") or "")

    filtered_count_stmt = select(func.count(User.id)).select_from(User).join(
        UserActivityStat, UserActivityStat.user_id == User.id, isouter=True
    )
    if filters:
        filtered_count_stmt = filtered_count_stmt.where(*filters)
    filtered_total_users = int(db.execute(filtered_count_stmt).scalar() or 0)

    total_users = int(db.execute(select(func.count(User.id))).scalar() or 0)
    total_admin_users = int(db.execute(select(func.count(User.id)).where(User.is_admin.is_(True))).scalar() or 0)
    total_disabled_users = int(
        db.execute(select(func.count(UserPassword.user_id)).where(UserPassword.disabled_at.is_not(None))).scalar() or 0
    )
    total_online_users = int(
        db.execute(select(func.count(UserActivityStat.user_id)).where(UserActivityStat.last_seen_at >= online_cutoff)).scalar() or 0
    )
    usage_sums = db.execute(
        select(
            func.coalesce(func.sum(UserUsageStat.total_generation_calls), 0),
            func.coalesce(func.sum(UserUsageStat.total_generation_error_calls), 0),
            func.coalesce(func.sum(UserUsageStat.total_generated_chars), 0),
        )
    ).one()
    total_generation_calls = int(usage_sums[0] or 0)
    total_generation_error_calls = int(usage_sums[1] or 0)
    total_generated_chars = int(usage_sums[2] or 0)

    return ok_payload(
        request_id=request_id,
        data={
            "users": users,
            "pagination": {
                "limit": int(limit),
                "cursor": cursor_value,
                "next_cursor": next_cursor,
                "has_more": has_more,
            },
            "summary": {
                "generated_at": now,
                "online_window_seconds": int(settings.auth_online_window_seconds or 300),
                "total_users": total_users,
                "total_admin_users": total_admin_users,
                "total_disabled_users": total_disabled_users,
                "total_online_users": total_online_users,
                "filtered_total_users": filtered_total_users,
                "total_generation_calls": total_generation_calls,
                "total_generation_error_calls": total_generation_error_calls,
                "total_generated_chars": total_generated_chars,
            },
        },
    )


@router.post("/auth/admin/users")
def create_user(request: Request, db: DbDep, user_id: AuthenticatedUserIdDep, body: AdminCreateUserRequest) -> dict:
    request_id = request.state.request_id
    _require_admin(db, user_id=user_id)

    target_user_id = body.user_id.strip()
    if not target_user_id:
        raise AppError.validation("user_id 不能为空")
    if db.get(User, target_user_id) is not None:
        raise AppError.conflict("用户已存在")

    user = User(
        id=target_user_id,
        email=(body.email or "").strip() or None,
        display_name=(body.display_name or "").strip() or None,
        is_admin=bool(body.is_admin),
    )
    db.add(user)

    raw_password = (body.password or "").strip()
    generated_password: str | None = None
    if not raw_password:
        generated_password = secrets.token_urlsafe(12)
        raw_password = generated_password

    pwd = UserPassword(
        user_id=target_user_id,
        password_hash=hash_password(raw_password),
        password_updated_at=utc_now(),
        disabled_at=None,
    )
    db.add(pwd)
    commit_user_creation(db)

    return ok_payload(
        request_id=request_id,
        data={"user": _user_admin_public(user=user, pwd=pwd), "temp_password": generated_password},
    )


@router.post("/auth/admin/users/{target_user_id}/password/reset")
def reset_user_password(
    request: Request,
    db: DbDep,
    user_id: AuthenticatedUserIdDep,
    target_user_id: str,
    body: AdminResetPasswordRequest,
) -> dict:
    request_id = request.state.request_id
    _require_admin(db, user_id=user_id)

    user = db.get(User, target_user_id)
    if user is None:
        raise AppError.not_found()

    raw_password = (body.new_password or "").strip()
    if not raw_password:
        raw_password = secrets.token_urlsafe(12)

    pwd = db.get(UserPassword, target_user_id)
    if pwd is None:
        pwd = UserPassword(user_id=target_user_id, password_hash="", password_updated_at=utc_now(), disabled_at=None)
        db.add(pwd)

    pwd.password_hash = hash_password(raw_password)
    pwd.password_updated_at = utc_now()
    db.commit()

    return ok_payload(request_id=request_id, data={"temp_password": raw_password})


@router.post("/auth/refresh")
def refresh_session(request: Request, user_id: AuthenticatedUserIdDep) -> JSONResponse:
    request_id = request.state.request_id
    expires_at = getattr(request.state, "session_expire_at", None)
    if expires_at is None:
        raise AppError.unauthorized()

    now = utc_now()
    remaining_seconds = int((expires_at - now).total_seconds())

    refreshed = False
    out_expires_at = expires_at
    if remaining_seconds <= settings.auth_refresh_threshold_seconds:
        refreshed = True
        out_expires_at = now + timedelta(seconds=settings.auth_session_ttl_seconds)

    response = JSONResponse(
        ok_payload(
            request_id=request_id,
            data={
                "refreshed": refreshed,
                "session": {"expire_at": int(out_expires_at.astimezone(timezone.utc).timestamp())},
            },
        )
    )
    if refreshed:
        set_session_cookies(response, user_id=user_id, expires_at=out_expires_at)
    return response


@router.post("/auth/logout")
def logout(request: Request) -> JSONResponse:
    request_id = request.state.request_id
    response = JSONResponse(ok_payload(request_id=request_id, data={}))
    clear_session_cookies(response)
    return response
