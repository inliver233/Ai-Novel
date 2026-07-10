"""共享测试脚手架（tests/support）。

本包集中了过去散落在 ~62 个测试文件里复制粘贴的样板代码：

- :mod:`tests.support.db`    —— 内存 SQLite 引擎 + 会话工厂 + 选择性建表
- :mod:`tests.support.app`   —— FastAPI 测试 app 工厂（复用生产中间件/handler）
- :mod:`tests.support.seed`  —— 高频实体 seeding helper（user / auth cookie）
- :mod:`tests.support.llm`   —— LLM 调用打桩 helper

设计原则：测试 app 的中间件/异常处理语义以生产 ``app.main`` 为事实源，而不是以
某一份旧测试样板为事实源。既有 unittest 风格测试可渐进迁移到这些 helper；根级
``backend/conftest.py`` 仅提供最小 fixture，其余 helper 推荐显式导入。
"""

from tests.support.app import make_client, make_test_app
from tests.support.db import create_tables, make_session_factory, make_sqlite_engine
from tests.support.llm import make_recorded_result, patch_call_llm
from tests.support.seed import auth_cookies, login_session_cookie, seed_user

__all__ = [
    "auth_cookies",
    "create_tables",
    "login_session_cookie",
    "make_client",
    "make_recorded_result",
    "make_session_factory",
    "make_sqlite_engine",
    "make_test_app",
    "patch_call_llm",
    "seed_user",
]
