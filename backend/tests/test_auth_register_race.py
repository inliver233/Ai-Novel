"""认证注册路由竞态测试。

H7（项目情况完全分析.md）子项：``local_register`` 是 check-then-act——先
``db.get`` 查重（auth.py:354），后 ``db.commit``（:369），且未捕获 ``IntegrityError``。
并发同名注册时，第二个请求通过了查重检查，但在 commit 时撞唯一约束 →
``IntegrityError`` 冒泡 → 全局 ``SQLAlchemyError`` 处理器返回 500 DB_ERROR，
而非语义正确的 409 CONFLICT。本测试直接模拟该竞态结果（让 commit 抛
IntegrityError），断言【正确行为】应返回 409；当前实现有 bug，标 ``known_issue``。

载体用 ``make_test_app`` + 手动挂生产的 ``sqlalchemy_error_handler``：脚手架默认
只挂 ``AppError`` 处理器（刻意设计，让其他异常自然冒泡给测试），这里需要 DB
异常处理器在场以反映生产的 500 DB_ERROR 行为。挂 ``SQLAlchemyError`` 不会吞掉
测试中的编程错误（TypeError/AssertionError 等非 DB 异常仍冒泡）。
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.testclient import TestClient

from app.api.routes import auth as auth_routes
from app.db.session import get_db
from app.main import sqlalchemy_error_handler
from app.models.user import User
from app.models.user_password import UserPassword

from tests.support import (
    create_tables,
    make_session_factory,
    make_sqlite_engine,
    make_test_app,
)


# H7: register 未捕获 IntegrityError → 并发重复应 409 CONFLICT，当前 500 DB_ERROR
@pytest.mark.known_issue
def test_local_register_concurrent_duplicate_returns_conflict() -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, [User, UserPassword])
    app = make_test_app(factory, [auth_routes])
    # 复用生产的 DB 异常处理器，使 IntegrityError → 500 DB_ERROR（反映生产行为）
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_error_handler)

    # 模拟竞态：查重通过（表空 → db.get 返回 None），但 commit 时撞唯一约束
    # （另一并发请求已先插入同 user_id）。
    boomed = factory()

    def _boom(*_args, **_kw):
        raise IntegrityError(
            "simulated concurrent insert",
            {},
            Exception("UNIQUE constraint failed: users.id"),
        )

    boomed.commit = _boom

    def _override_get_db():
        try:
            yield boomed
        finally:
            boomed.close()

    app.dependency_overrides[get_db] = _override_get_db

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/auth/local/register",
        json={"user_id": "race-user", "password": "whatever-123"},
    )

    # 正确行为：并发重复注册应返回 409 CONFLICT。
    # 当前 bug：IntegrityError 未被路由捕获 → 500 DB_ERROR。
    assert resp.status_code == 409
    body = resp.json()
    assert body.get("ok") is False
    assert body["error"]["code"] == "CONFLICT"
