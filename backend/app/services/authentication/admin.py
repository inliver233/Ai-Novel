from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Request
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ok_payload
from app.db.datetime_compat import coerce_utc_datetime
from app.db.utils import utc_now
from app.models.user import User
from app.models.user_activity_stat import UserActivityStat
from app.models.user_password import UserPassword
from app.models.user_usage_stat import UserUsageStat
from app.schemas.auth import AdminCreateUserRequest, AdminResetPasswordRequest, DisableUserRequest
from app.services.authentication.passwords import commit_user_creation, hash_password


def _to_utc_epoch(value: datetime | None) -> float | None:
    if value is None:
        return None
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)).timestamp()


def user_admin_public(
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
        last_seen_epoch, cutoff_epoch = _to_utc_epoch(last_seen_at), _to_utc_epoch(online_cutoff)
        online = bool(last_seen_epoch is not None and cutoff_epoch is not None and last_seen_epoch >= cutoff_epoch)
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "is_admin": bool(user.is_admin),
        "disabled": bool(user.disabled_at is not None),
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


def set_user_disabled(request: Request, db: Session, target_user_id: str, body: DisableUserRequest) -> dict:
    user = db.get(User, target_user_id)
    if user is None:
        raise AppError.not_found()
    disabled_at = utc_now() if body.disabled else None
    if body.disabled:
        db.execute(
            update(User)
            .where(User.id == target_user_id)
            .values(
                disabled_at=disabled_at,
                session_invalid_before=disabled_at,
                session_version=User.session_version + 1,
                updated_at=disabled_at,
            )
        )
    else:
        user.disabled_at = None
    pwd = db.get(UserPassword, target_user_id)
    if pwd is not None:
        pwd.disabled_at = disabled_at
    db.commit()
    return ok_payload(request_id=request.state.request_id, data={})


def list_users(
    request: Request, db: Session, *, limit: int, cursor: str | None, q: str | None, online_only: bool
) -> dict:
    now = utc_now()
    online_cutoff = now - timedelta(seconds=int(settings.auth_online_window_seconds or 300))
    cursor_value = str(cursor or "").strip() or None
    q_value = str(q or "").strip().lower()
    filters = []
    if q_value:
        pattern = f"%{q_value}%"
        filters.append(
            or_(
                func.lower(User.id).like(pattern),
                func.lower(func.coalesce(User.display_name, "")).like(pattern),
                func.lower(func.coalesce(User.email, "")).like(pattern),
            )
        )
    if online_only:
        filters.extend((UserActivityStat.last_seen_at.is_not(None), UserActivityStat.last_seen_at >= online_cutoff))
    stmt = (
        select(User, UserPassword, UserActivityStat, UserUsageStat)
        .join(UserPassword, UserPassword.user_id == User.id, isouter=True)
        .join(UserActivityStat, UserActivityStat.user_id == User.id, isouter=True)
        .join(UserUsageStat, UserUsageStat.user_id == User.id, isouter=True)
    )
    if filters:
        stmt = stmt.where(*filters)
    if cursor_value:
        stmt = stmt.where(User.id > cursor_value)
    rows = db.execute(stmt.order_by(User.id.asc()).limit(limit + 1)).all()
    has_more = len(rows) > limit
    users = [
        user_admin_public(user=u, pwd=p, activity=a, usage=usg, online_cutoff=online_cutoff)
        for u, p, a, usg in rows[:limit]
    ]
    count_stmt = (
        select(func.count(User.id))
        .select_from(User)
        .join(UserActivityStat, UserActivityStat.user_id == User.id, isouter=True)
    )
    if filters:
        count_stmt = count_stmt.where(*filters)
    usage_sums = db.execute(
        select(
            func.coalesce(func.sum(UserUsageStat.total_generation_calls), 0),
            func.coalesce(func.sum(UserUsageStat.total_generation_error_calls), 0),
            func.coalesce(func.sum(UserUsageStat.total_generated_chars), 0),
        )
    ).one()
    return ok_payload(
        request_id=request.state.request_id,
        data={
            "users": users,
            "pagination": {
                "limit": int(limit),
                "cursor": cursor_value,
                "next_cursor": str(users[-1].get("id") or "") if has_more and users else None,
                "has_more": has_more,
            },
            "summary": {
                "generated_at": now,
                "online_window_seconds": int(settings.auth_online_window_seconds or 300),
                "total_users": int(db.execute(select(func.count(User.id))).scalar() or 0),
                "total_admin_users": int(
                    db.execute(select(func.count(User.id)).where(User.is_admin.is_(True))).scalar() or 0
                ),
                "total_disabled_users": int(
                    db.execute(select(func.count(User.id)).where(User.disabled_at.is_not(None))).scalar() or 0
                ),
                "total_online_users": int(
                    db.execute(
                        select(func.count(UserActivityStat.user_id)).where(
                            UserActivityStat.last_seen_at >= online_cutoff
                        )
                    ).scalar()
                    or 0
                ),
                "filtered_total_users": int(db.execute(count_stmt).scalar() or 0),
                "total_generation_calls": int(usage_sums[0] or 0),
                "total_generation_error_calls": int(usage_sums[1] or 0),
                "total_generated_chars": int(usage_sums[2] or 0),
            },
        },
    )


def create_user(request: Request, db: Session, body: AdminCreateUserRequest) -> dict:
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
    generated_password = None
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
        request_id=request.state.request_id,
        data={"user": user_admin_public(user=user, pwd=pwd), "temp_password": generated_password},
    )


def reset_user_password(request: Request, db: Session, target_user_id: str, body: AdminResetPasswordRequest) -> dict:
    if db.get(User, target_user_id) is None:
        raise AppError.not_found()
    raw_password = (body.new_password or "").strip() or secrets.token_urlsafe(12)
    pwd = db.get(UserPassword, target_user_id)
    if pwd is None:
        pwd = UserPassword(user_id=target_user_id, password_hash="", password_updated_at=utc_now(), disabled_at=None)
        db.add(pwd)
    pwd.password_hash = hash_password(raw_password)
    pwd.password_updated_at = utc_now()
    db.commit()
    return ok_payload(request_id=request.state.request_id, data={"temp_password": raw_password})
