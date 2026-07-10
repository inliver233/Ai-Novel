"""``create_chapters`` 的 LLM API key Header 长度契约回归测试。

该端点与其他细纲生成端点统一使用规范名称 ``X-LLM-API-Key``，允许最多
4096 个字符。超长值必须在进入端点前由 FastAPI 校验拒绝，并通过生产校验
错误处理器返回不包含敏感输入的 ``400 VALIDATION_ERROR`` 信封。
"""

from __future__ import annotations

import json

import pytest
from fastapi.exceptions import RequestValidationError

from app.api.routes import detailed_outlines as detailed_outlines_routes
from app.main import validation_error_handler
from app.models.chapter import Chapter
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


def test_create_chapters_rejects_overlength_llm_api_key_header(env):
    """4097 字符的密钥在路由执行和数据库变更前被安全拒绝。"""
    secret_marker = "sk-test-sensitive-marker"
    overlength_key = secret_marker + ("x" * (4097 - len(secret_marker)))
    resp = env["client"].post(
        f"/api/detailed_outlines/{DETAILED_OUTLINE_ID}/create_chapters",
        headers={"X-LLM-API-Key": overlength_key},
    )

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    errors = body["error"]["details"]["errors"]
    assert any(
        error.get("type") == "string_too_long"
        and error.get("loc") == ["header", "X-LLM-API-Key"]
        for error in errors
    )
    assert secret_marker not in resp.text
    assert overlength_key not in resp.text

    with env["factory"]() as db:
        assert db.query(Chapter).count() == 0


def test_create_chapters_accepts_max_length_llm_api_key_header(env):
    """恰好 4096 字符的密钥仍满足公开 Header 契约。"""
    resp = env["client"].post(
        f"/api/detailed_outlines/{DETAILED_OUTLINE_ID}/create_chapters",
        headers={"X-LLM-API-Key": "x" * 4096},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["count"] == 1
