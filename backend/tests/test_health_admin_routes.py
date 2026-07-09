"""health 与 admin 用户管理端点 happy-path（D 类安全网）。

覆盖两部分当前正确的活功能：

1. ``GET /api/health`` —— 健康检查（无需登录、无 DB 依赖）。
2. ``app/api/routes/auth.py`` 的 admin 用户管理端点（需 admin 登录）：

     GET    /api/auth/admin/users
     POST   /api/auth/admin/users
     POST   /api/auth/admin/users/{id}/disable
     POST   /api/auth/admin/users/{id}/password/reset

断言结构化字段，不锁中文文案；每条真触达端点断言真实响应。
"""

from __future__ import annotations

from app.api.routes import auth as auth_routes
from app.api.routes import health as health_routes
from app.models.user import User
from app.models.user_activity_stat import UserActivityStat
from app.models.user_password import UserPassword
from app.models.user_usage_stat import UserUsageStat

from tests.support import (
    auth_cookies,
    create_tables,
    make_client,
    make_session_factory,
    make_sqlite_engine,
    make_test_app,
    seed_user,
)


# ── health ───────────────────────────────────────────────────────────


def test_health_returns_ok_and_status() -> None:
    """GET /api/health 无需登录，返回 ok 信封 + status/version 及队列观测键。"""
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    app = make_test_app(factory, [health_routes])
    client = make_client(app)

    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["status"] == "healthy"
    assert "version" in data
    # queue_backend 由 get_queue_status_for_health 注入；redis_ok 在无 Redis 的
    # 测试环境为 False 属正常 happy-path，不在此断言其值。
    assert "queue_backend" in data


# ── admin 用户管理 ───────────────────────────────────────────────────

# list_users 对 UserActivityStat / UserUsageStat 做左连接，两表必须存在。
_ADMIN_MODELS = [User, UserPassword, UserActivityStat, UserUsageStat]


def _new_admin_client():
    """构造已建表 + admin1(is_admin) 已登录的测试 client 与 factory。"""
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, _ADMIN_MODELS)
    seed_user(factory, user_id="admin1", is_admin=True)
    app = make_test_app(factory, [auth_routes])
    client = make_client(app)
    auth_cookies(client, "admin1")
    return client, factory


def test_admin_list_users_returns_structure() -> None:
    """GET /api/auth/admin/users 返回 users/pagination/summary 结构。"""
    client, _ = _new_admin_client()

    resp = client.get("/api/auth/admin/users")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert "users" in data
    assert "pagination" in data
    assert "summary" in data
    # admin1 自己应在列表里
    assert any(u["id"] == "admin1" for u in data["users"])
    # 分页结构键
    for key in ("limit", "cursor", "next_cursor", "has_more"):
        assert key in data["pagination"]
    # summary 关键计数键
    for key in ("total_users", "total_admin_users", "total_disabled_users"):
        assert key in data["summary"]


def test_admin_create_user_with_password_returns_user_without_temp_password() -> None:
    """POST /api/auth/admin/users 显式传密码时 temp_password 为 None。"""
    client, _ = _new_admin_client()

    resp = client.post(
        "/api/auth/admin/users",
        json={
            "user_id": "newbie",
            "password": "Secret-pw-1",
            "display_name": "New User",
            "is_admin": False,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["user"]["id"] == "newbie"
    assert data["user"]["is_admin"] is False
    assert data["temp_password"] is None


def test_admin_create_user_without_password_generates_temp_password() -> None:
    """POST /api/auth/admin/users 未传密码时自动生成 temp_password。"""
    client, _ = _new_admin_client()

    resp = client.post(
        "/api/auth/admin/users",
        json={"user_id": "auto_pw"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["user"]["id"] == "auto_pw"
    assert isinstance(data["temp_password"], str) and len(data["temp_password"]) > 0


def test_admin_disable_then_enable_user_toggles_disabled_at() -> None:
    """POST /api/auth/admin/users/{id}/disable 切换 disabled 标记。"""
    client, factory = _new_admin_client()
    client.post("/api/auth/admin/users", json={"user_id": "victim", "password": "pw-12345"})

    resp_off = client.post(
        "/api/auth/admin/users/victim/disable",
        json={"disabled": True},
    )
    assert resp_off.status_code == 200
    assert resp_off.json()["ok"] is True
    with factory() as db:
        pwd = db.get(UserPassword, "victim")
        assert pwd is not None
        assert pwd.disabled_at is not None

    resp_on = client.post(
        "/api/auth/admin/users/victim/disable",
        json={"disabled": False},
    )
    assert resp_on.status_code == 200
    assert resp_on.json()["ok"] is True
    with factory() as db:
        pwd = db.get(UserPassword, "victim")
        assert pwd is not None
        assert pwd.disabled_at is None


def test_admin_reset_user_password_returns_temp_password() -> None:
    """POST /api/auth/admin/users/{id}/password/reset 返回新 temp_password。"""
    client, _ = _new_admin_client()
    client.post("/api/auth/admin/users", json={"user_id": "forgetful", "password": "old-pw-12"})

    resp = client.post(
        "/api/auth/admin/users/forgetful/password/reset",
        json={"new_password": "Brand-new-pw-2"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["temp_password"] == "Brand-new-pw-2"
