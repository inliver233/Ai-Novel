"""全局错误信封契约测试。

M2（项目情况完全分析.md）曾由于未注册 ``StarletteHTTPException``
全局处理器，使 404（路由不存在）/405（方法不允许）绕过项目统一错误
信封。本测试固化修复后的响应体、请求 ID 和 HTTP 协议头契约。

载体用生产 ``app``（完整 exception handler 栈），而非 ``make_test_app`` 脚手架——
脚手架只挂 ``AppError`` 处理器是刻意设计（让其他异常自然冒泡给测试），若给它加全栈
会吞掉现有测试的意外异常。用生产 app 则修复 ``main.py`` 后测试自动反映，无需双手工
对齐。404/405 在路由匹配阶段产生，不触达 DB，故无需 DB 脚手架。
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    """生产 app 的 TestClient（不触发 lifespan）。

    不进 ``with`` → 不运行 lifespan(startup 连 DB / alembic / bootstrap)；路由与
    exception handler 在 import 时已注册到模块级 ``app``，足以产生 404/405。
    """
    return TestClient(app)


# M2: StarletteHTTPException 必须转换为统一错误信封
def test_not_found_returns_unified_error_envelope(client: TestClient) -> None:
    request_id = "rid-error-envelope-404"
    resp = client.get(
        "/api/__definitely_not_a_route__",
        headers={"X-Request-Id": request_id},
    )

    assert resp.status_code == 404
    assert resp.json() == {
        "ok": False,
        "error": {
            "code": "NOT_FOUND",
            "message": "Not Found",
            "details": {},
        },
        "request_id": request_id,
    }
    assert resp.headers["X-Request-Id"] == request_id


# M2: 405 除统一信封外，还必须保留 Starlette 生成的 Allow 头
def test_method_not_allowed_returns_unified_error_envelope(client: TestClient) -> None:
    request_id = "rid-error-envelope-405"
    resp = client.post(
        "/api/health",  # GET 路由用 POST → 405
        headers={"X-Request-Id": request_id},
    )

    assert resp.status_code == 405
    assert resp.json() == {
        "ok": False,
        "error": {
            "code": "METHOD_NOT_ALLOWED",
            "message": "Method Not Allowed",
            "details": {},
        },
        "request_id": request_id,
    }
    assert resp.headers["X-Request-Id"] == request_id
    assert "GET" in {method.strip() for method in resp.headers["Allow"].split(",")}
