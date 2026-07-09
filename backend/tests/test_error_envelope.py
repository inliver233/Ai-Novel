"""全局错误信封契约测试。

M2（项目情况完全分析.md）：``StarletteHTTPException`` 未注册全局处理器 →
404（路由不存在）/405（方法不允许）会走 Starlette 内置默认处理器，绕过项目统一
错误信封 ``{ok, error, request_id}``，返回 ``{"detail": "..."}``。本测试断言
【正确行为】：4xx/5xx 响应都应是统一信封。当前实现有 bug，故标记 ``known_issue``
（诚实镜像语义：真跑真红；修复后自动转绿，从 bug 看板毕业）。

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


# M2: StarletteHTTPException 未注册 → 404 绕过统一错误信封
@pytest.mark.known_issue
def test_not_found_returns_unified_error_envelope(client: TestClient) -> None:
    resp = client.get("/api/__definitely_not_a_route__")
    assert resp.status_code == 404
    body = resp.json()
    # 正确行为：统一信封 {ok:false, error:{code,message,details}, request_id}
    # 当前 bug：返回 Starlette 默认 {"detail": "Not Found"}，缺 ok/error/request_id。
    assert body.get("ok") is False
    assert isinstance(body.get("error"), dict)
    assert {"code", "message"} <= set(body["error"])
    assert "request_id" in body


# M2: StarletteHTTPException 未注册 → 405 绕过统一错误信封
@pytest.mark.known_issue
def test_method_not_allowed_returns_unified_error_envelope(client: TestClient) -> None:
    resp = client.post("/api/health")  # GET 路由用 POST → 405
    assert resp.status_code == 405
    body = resp.json()
    # 当前 bug：返回 {"detail": "Method Not Allowed"}，缺统一信封字段。
    assert body.get("ok") is False
    assert isinstance(body.get("error"), dict)
    assert {"code", "message"} <= set(body["error"])
    assert "request_id" in body
