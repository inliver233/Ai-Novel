"""B 类 known_issue 测试 —— detailed_outlines create_chapters 端点
X-LLM-Api-Key 请求头缺少 max_length=4096 校验（catalog M12）。

【诚实镜像】断言【正确行为】：超过 4096 字符的 X-LLM-Api-Key 应被
FastAPI Header 校验拒绝，返回 400 VALIDATION_ERROR。
当前实现（app/api/routes/detailed_outlines.py:522）该 Header 缺少
max_length=4096——同文件 line 349/485/653 的同类端点均有该校验——故
超长输入被接受，端点继续执行（200），测试红（FAILED）。
bug 修复后（补 max_length=4096）→ 400 VALIDATION_ERROR → 测试绿。
"""

from __future__ import annotations

import json

import pytest
from fastapi.exceptions import RequestValidationError

from app.api.routes import detailed_outlines as detailed_outlines_routes
from app.main import validation_error_handler
from app.models.detailed_outline import DetailedOutline
from app.models.outline import Outline
from app.models.project import Project
from tests.support import (
    auth_cookies,
    create_tables,
    make_client,
    make_session_factory,
    make_sqlite_engine,
    make_test_app,
    seed_user,
)

PROJECT_ID = "p1"
OUTLINE_ID = "o1"
USER_ID = "u1"
DETAILED_OUTLINE_ID = "d1"


@pytest.fixture
def env():
    # M12：每个用例独立的内存 DB + 已登录 client（owner=u1，含 project + outline + 细纲）。
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine)
    seed_user(factory, user_id=USER_ID)

    with factory() as db:
        db.add(Project(id=PROJECT_ID, owner_user_id=USER_ID, name="测试项目", active_outline_id=OUTLINE_ID))
        db.add(Outline(id=OUTLINE_ID, project_id=PROJECT_ID, title="测试大纲", content_md=""))
        # 预建带 structure_json（含 chapters）的细纲，使 create_chapters 走
        # needs_generation=False 分支（不触达 LLM），让超长 Header 成为唯一变量。
        db.add(
            DetailedOutline(
                id=DETAILED_OUTLINE_ID,
                outline_id=OUTLINE_ID,
                project_id=PROJECT_ID,
                volume_number=1,
                volume_title="第一卷",
                status="done",
                structure_json=json.dumps({"chapters": [{"number": 1, "title": "第一章", "summary": "开篇"}]}),
            )
        )
        db.commit()

    app = make_test_app(factory, [detailed_outlines_routes])
    # 注册生产 VALIDATION_ERROR 处理器，使 Header max_length 校验失败时返回
    # 400（与 app.main 生产行为一致），而非 FastAPI 默认 422。
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    client = make_client(app)
    auth_cookies(client, USER_ID)

    yield {"client": client, "factory": factory}
    engine.dispose()


@pytest.mark.known_issue  # M12
def test_create_chapters_rejects_overlength_llm_api_key_header(env):
    """X-LLM-Api-Key 超过 4096 字符应返回 400 VALIDATION_ERROR。

    正确行为：该 Header 应有 max_length=4096（与同文件 line 485 等同类端点一致），
    超长值被 FastAPI Header 校验拒绝 → 400 VALIDATION_ERROR。
    当前 bug：line 522 缺 max_length → 超长值被接受 → 端点继续执行（200）→ 断言失败（红）。
    """
    overlength_key = "x" * 5000  # 超过 4096 上限
    resp = env["client"].post(
        f"/api/detailed_outlines/{DETAILED_OUTLINE_ID}/create_chapters",
        headers={"X-LLM-Api-Key": overlength_key},
    )

    # 断言【正确行为】：400 校验错误（统一错误信封 ok=False + error.code=VALIDATION_ERROR）
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
