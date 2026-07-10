"""FastAPI 测试 app 工厂。

替换旧测试里 29 份复制的 ``_make_test_app``。复用生产中间件 / 异常处理器，
使路由测试能触达真实的认证会话中间件与统一错误信封行为——这正是逐个测试
复制这套中间件栈的原因，集中维护后只需改一处。
"""

from __future__ import annotations

from typing import Callable, Generator, Iterable

from fastapi import FastAPI, Request
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from app.core.errors import AppError
from app.db.session import get_db
from app.main import app_error_handler, auth_session_middleware


def make_test_app(
    session_factory: sessionmaker,
    routers: Iterable[object] = (),
    *,
    with_auth_middleware: bool = True,
    with_request_id: bool = True,
) -> FastAPI:
    """构建一个挂载真实中间件/handler 栈的最小 FastAPI app。

    参数：
      - ``session_factory``：测试的内存 SQLite 会话工厂。
      - ``routers``：需挂载到 ``/api`` 前缀下的路由模块（每个是带 ``router`` 属性的模块对象，
        或直接传 ``router`` 实例均可——参见下方 ``_include`` 处理）。
      - ``with_auth_middleware``：是否挂载真实的 cookie 会话认证中间件
        （``app.main.auth_session_middleware``）。默认 True，使路由测试能覆盖
        未登录/已登录/dev 回退等真实路径。
      - ``with_request_id``：是否附加固定 ``request_id="rid-test"`` 中间件，
        便于断言响应头 ``X-Request-Id`` 与错误信封里的 ``request_id``。

    依赖注入：``get_db`` 被 override 为从 ``session_factory`` 取会话的生成器。
    """
    app = FastAPI()

    # Starlette 后注册的 middleware 位于外层。生产 main.py 先注册 auth、后注册
    # request-id/logging，因此这里也必须先 auth 后 request-id，使 request-id 在认证
    # 异常路径上同样可用。
    if with_auth_middleware:
        app.middleware("http")(auth_session_middleware)

    if with_request_id:

        @app.middleware("http")
        async def _request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
            request.state.request_id = "rid-test"
            return await call_next(request)

    app.add_exception_handler(AppError, app_error_handler)

    for router in routers:
        _include(app, router)

    def _override_get_db() -> Generator[Session, None, None]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    return app


def _include(app: FastAPI, router: object) -> None:
    """挂载一个 router，兼容传入模块对象或 router 实例两种写法。"""
    obj = getattr(router, "router", router)
    app.include_router(obj, prefix="/api")


def make_client(app: FastAPI) -> TestClient:
    """为给定 app 创建一个 Starlette ``TestClient``。"""
    return TestClient(app)
