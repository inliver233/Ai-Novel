"""高频实体 seeding helper 与认证 cookie 工具。

``seed_user`` 复刻自 ``tests/test_auth_session.py:87-98`` 的高频模式：
创建一个 ``User`` + 一条 ``UserPassword``（bcrypt 哈希），随后即可登录或
直接注入会话 cookie 跳过登录流程。
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from app.core.auth_session import encode_session_cookie
from app.core.config import settings
from app.db.utils import utc_now
from app.models.user import User
from app.models.user_password import UserPassword
from app.services.auth_service import hash_password

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from starlette.testclient import TestClient


def seed_user(
    db: "Session | sessionmaker",
    *,
    user_id: str = "u1",
    password: str = "password123",
    is_admin: bool = False,
    disabled: bool = False,
    display_name: str | None = None,
) -> User:
    """创建一个用户（含密码记录）并提交。

    既接受已打开的 ``Session``（在调用方事务里 add+commit），也接受 ``sessionmaker``
    （内部调用其建立会话并提交）。区分方式：``Session`` 有 ``.add``，``sessionmaker`` 没有。
    """
    if hasattr(db, "add"):
        session = db  # type: ignore[assignment]
        own = False
    else:
        session = db()  # type: ignore[operator]
        own = True
    try:
        _create_and_commit(session, user_id, password, is_admin, disabled, display_name)
        return session.get(User, user_id)  # type: ignore[return-value]
    finally:
        if own:
            session.close()


def _create_and_commit(
    db: "Session",
    user_id: str,
    password: str,
    is_admin: bool,
    disabled: bool,
    display_name: str | None,
) -> None:
    db.add(
        User(
            id=user_id,
            display_name=display_name or user_id,
            is_admin=is_admin,
            disabled_at=utc_now() if disabled else None,
        )
    )
    db.add(
        UserPassword(
            user_id=user_id,
            password_hash=hash_password(password),
            disabled_at=utc_now() if disabled else None,
        )
    )
    db.commit()


def login_session_cookie(
    user_id: str = "u1",
    *,
    ttl_seconds: int | None = None,
) -> str:
    """生成一个已签名的会话 cookie 值（跳过登录流程）。

    用于 ``client.cookies.set(settings.auth_cookie_user_id_name, login_session_cookie(...))``，
    模拟"已登录"状态而无需走 ``/api/auth/local/login``。
    """
    ttl = ttl_seconds if ttl_seconds is not None else int(settings.auth_session_ttl_seconds)
    expires_at = utc_now() + timedelta(seconds=ttl)
    return encode_session_cookie(user_id=user_id, expires_at=expires_at)


def auth_cookies(client: "TestClient", user_id: str = "u1", *, ttl_seconds: int | None = None) -> "TestClient":
    """为 ``client`` 注入已登录的会话 cookie，并返回该 client（便于链式调用）。"""
    client.cookies.set(settings.auth_cookie_user_id_name, login_session_cookie(user_id, ttl_seconds=ttl_seconds))
    return client
