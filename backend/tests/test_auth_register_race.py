"""本地注册与管理员创建用户的提交冲突测试。

H7（项目情况完全分析.md）子项：``local_register`` 是 check-then-act——先
``db.get`` 查重，后 ``db.commit``；历史实现未捕获提交阶段的 ``IntegrityError``。
并发同名注册时，第二个请求可能通过查重检查，但在 commit 时撞唯一约束。本文件
不伪造线程级并发，而是在数据库提交边界确定性模拟“竞态失败方”：让 commit 抛出
``IntegrityError``，断言两条创建路径都回滚事务并返回语义正确的 409 CONFLICT；
同时用真实 SQLite 唯一约束证明失败事务不会遗留用户或密码行。

载体用 ``make_test_app`` + 手动挂生产的 ``sqlalchemy_error_handler``：脚手架默认
只挂 ``AppError`` 处理器（刻意设计，让其他异常自然冒泡给测试），这里需要 DB
异常处理器在场以反映生产的 500 DB_ERROR 行为。挂 ``SQLAlchemyError`` 不会吞掉
测试中的编程错误（TypeError/AssertionError 等非 DB 异常仍冒泡）。
"""

from __future__ import annotations

from unittest.mock import Mock

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.testclient import TestClient

from app.api.routes import auth as auth_routes
from app.db.session import get_db
from app.main import sqlalchemy_error_handler
from app.models.user import User
from app.models.user_password import UserPassword

from tests.support import (
    auth_cookies,
    create_tables,
    make_session_factory,
    make_sqlite_engine,
    make_test_app,
    seed_user,
)


def _client_with_commit_failure(
    error: SQLAlchemyError,
    *,
    authenticated_admin: bool = False,
) -> tuple[TestClient, Mock, Engine]:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, [User, UserPassword])
    if authenticated_admin:
        seed_user(factory, user_id="admin1", is_admin=True)
    app = make_test_app(factory, [auth_routes])
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_error_handler)

    boomed = factory()
    rollback = Mock(wraps=boomed.rollback)

    def _boom(*_args, **_kw):
        raise error

    boomed.commit = _boom
    boomed.rollback = rollback

    def _override_get_db():
        try:
            yield boomed
        finally:
            boomed.close()

    app.dependency_overrides[get_db] = _override_get_db

    client = TestClient(app, raise_server_exceptions=False)
    if authenticated_admin:
        auth_cookies(client, "admin1")
    return client, rollback, engine


# H7: 确定性模拟并发竞态的失败方；保留原 node 名用于 catalog 连续性。
def test_local_register_concurrent_duplicate_returns_conflict() -> None:
    error = IntegrityError(
        "simulated concurrent insert",
        {},
        Exception("UNIQUE constraint failed: users.id"),
    )
    client, rollback, engine = _client_with_commit_failure(error)
    try:
        resp = client.post(
            "/api/auth/local/register",
            json={"user_id": "race-user", "password": "whatever-123"},
        )
    finally:
        engine.dispose()

    assert resp.status_code == 409
    body = resp.json()
    assert body == {
        "ok": False,
        "error": {"code": "CONFLICT", "message": "用户已存在", "details": {}},
        "request_id": "rid-test",
    }
    assert resp.headers["X-Request-Id"] == "rid-test"
    serialized = str(body).lower()
    assert "unique" not in serialized
    assert "constraint" not in serialized
    assert "users.id" not in serialized
    rollback.assert_called_once_with()


def test_local_register_non_integrity_database_error_remains_db_error() -> None:
    client, rollback, engine = _client_with_commit_failure(
        SQLAlchemyError("simulated database outage")
    )
    try:
        resp = client.post(
            "/api/auth/local/register",
            json={"user_id": "db-error-user", "password": "whatever-123"},
        )
    finally:
        engine.dispose()

    assert resp.status_code == 500
    assert resp.json() == {
        "ok": False,
        "error": {"code": "DB_ERROR", "message": "数据库错误", "details": {}},
        "request_id": "rid-test",
    }
    assert resp.headers["X-Request-Id"] == "rid-test"
    rollback.assert_not_called()


def test_local_register_unique_email_conflict_rolls_back_cleanly() -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, [User, UserPassword])
    app = make_test_app(factory, [auth_routes])
    client = TestClient(app)
    try:
        first = client.post(
            "/api/auth/local/register",
            json={"user_id": "email-owner", "email": "shared@example.com", "password": "whatever-123"},
        )
        duplicate = client.post(
            "/api/auth/local/register",
            json={"user_id": "email-racer", "email": "shared@example.com", "password": "whatever-123"},
        )
    finally:
        engine.dispose()

    assert first.status_code == 200
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "CONFLICT"


def test_admin_create_user_integrity_error_returns_sanitized_conflict() -> None:
    error = IntegrityError(
        "simulated concurrent insert",
        {},
        Exception("UNIQUE constraint failed: users.email; secret@example.com"),
    )
    client, rollback, engine = _client_with_commit_failure(
        error,
        authenticated_admin=True,
    )
    try:
        resp = client.post(
            "/api/auth/admin/users",
            json={
                "user_id": "race-user",
                "email": "secret@example.com",
                "password": "whatever-123",
            },
        )
    finally:
        engine.dispose()

    assert resp.status_code == 409
    body = resp.json()
    assert body == {
        "ok": False,
        "error": {"code": "CONFLICT", "message": "用户已存在", "details": {}},
        "request_id": "rid-test",
    }
    assert resp.headers["X-Request-Id"] == "rid-test"
    serialized = str(body).lower()
    assert "unique" not in serialized
    assert "constraint" not in serialized
    assert "users.email" not in serialized
    assert "secret@example.com" not in serialized
    rollback.assert_called_once_with()


def test_admin_create_user_non_integrity_database_error_remains_db_error() -> None:
    client, rollback, engine = _client_with_commit_failure(
        SQLAlchemyError("simulated database outage"),
        authenticated_admin=True,
    )
    try:
        resp = client.post(
            "/api/auth/admin/users",
            json={"user_id": "db-error-user", "password": "whatever-123"},
        )
    finally:
        engine.dispose()

    assert resp.status_code == 500
    assert resp.json() == {
        "ok": False,
        "error": {"code": "DB_ERROR", "message": "数据库错误", "details": {}},
        "request_id": "rid-test",
    }
    assert resp.headers["X-Request-Id"] == "rid-test"
    rollback.assert_not_called()


def test_admin_create_user_duplicate_email_leaves_no_partial_credentials() -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, [User, UserPassword])
    seed_user(factory, user_id="admin1", is_admin=True)
    app = make_test_app(factory, [auth_routes])
    client = TestClient(app)
    auth_cookies(client, "admin1")
    try:
        first = client.post(
            "/api/auth/admin/users",
            json={
                "user_id": "email-owner",
                "email": "shared@example.com",
                "password": "whatever-123",
            },
        )
        duplicate = client.post(
            "/api/auth/admin/users",
            json={
                "user_id": "email-racer",
                "email": "shared@example.com",
                "password": "whatever-456",
            },
        )

        assert first.status_code == 200
        assert duplicate.status_code == 409
        body = duplicate.json()
        assert body == {
            "ok": False,
            "error": {"code": "CONFLICT", "message": "用户已存在", "details": {}},
            "request_id": "rid-test",
        }
        assert "shared@example.com" not in str(body).lower()

        with factory() as db:
            assert db.get(User, "email-owner") is not None
            assert db.get(UserPassword, "email-owner") is not None
            assert db.get(User, "email-racer") is None
            assert db.get(UserPassword, "email-racer") is None
    finally:
        engine.dispose()
