"""认证本地登录/注册 happy-path（D 类安全网）。

覆盖核心认证链路：注册成功 → 登录成功 → 登录失败（错密码 / 未知用户 → 401）。
这些都是当前正确的活功能，测试应保持绿，构成 auth 域的安全网。

注意 ``local_login`` 对"用户不存在"与"密码错"返回同样的 401（防用户枚举，安全最佳
实践），本测试覆盖两种情形都应得 401。
"""

from __future__ import annotations

from app.api.routes import auth as auth_routes
from app.models.user import User
from app.models.user_password import UserPassword

from tests.support import (
    create_tables,
    make_client,
    make_session_factory,
    make_sqlite_engine,
    make_test_app,
)


def _new_app():
    """隔离的 auth 测试 app（内存 SQLite + 仅 auth 路由）。"""
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, [User, UserPassword])
    return make_test_app(factory, [auth_routes])


def test_local_register_returns_user_and_session() -> None:
    app = _new_app()
    client = make_client(app)
    resp = client.post(
        "/api/auth/local/register",
        json={"user_id": "alice", "password": "correct-123"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["user"]["id"] == "alice"
    assert "expire_at" in body["data"]["session"]


def test_local_login_succeeds_after_register() -> None:
    app = _new_app()
    client = make_client(app)
    client.post(
        "/api/auth/local/register",
        json={"user_id": "alice", "password": "correct-123"},
    )
    resp = client.post(
        "/api/auth/local/login",
        json={"user_id": "alice", "password": "correct-123"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["user"]["id"] == "alice"


def test_local_login_wrong_password_returns_unauthorized() -> None:
    app = _new_app()
    client = make_client(app)
    client.post(
        "/api/auth/local/register",
        json={"user_id": "alice", "password": "correct-123"},
    )
    resp = client.post(
        "/api/auth/local/login",
        json={"user_id": "alice", "password": "wrong-456"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "UNAUTHORIZED"


def test_local_login_unknown_user_returns_unauthorized() -> None:
    app = _new_app()
    client = make_client(app)
    resp = client.post(
        "/api/auth/local/login",
        json={"user_id": "ghost", "password": "whatever"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"
