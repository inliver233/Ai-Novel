"""D 类 happy-path 测试：chapters 域路由。

断言【当前正确行为】，所有用例应绿（安全网）。仅覆盖主要 CRUD/读取/排序类
端点；依赖外部 LLM 的生成/规划/流式端点（plan / generate / generate-precheck
/ generate-stream）不在本文件覆盖范围内。

项目归属：Project.owner_user_id="u1" 即通过 require_project_editor/viewer
的 "owner" 角色校验（见 app/api/deps.py:_project_role）。
"""

from __future__ import annotations

import pytest

from app.api.routes import chapters as chapters_routes
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


@pytest.fixture
def env():
    """每个用例独立的内存 DB + 已登录 client（owner=u1，含 active outline）。"""
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    # chapters 的 create/update/delete 会 fail-soft 地写 ProjectSettings /
    # ProjectTask 等表（schedule_*_task），故建全部已注册表，避免缺表报错。
    create_tables(engine)
    seed_user(factory, user_id=USER_ID)

    with factory() as db:
        db.add(Project(id=PROJECT_ID, owner_user_id=USER_ID, name="测试项目", active_outline_id=OUTLINE_ID))
        db.add(Outline(id=OUTLINE_ID, project_id=PROJECT_ID, title="测试大纲", content_md=""))
        db.commit()

    app = make_test_app(factory, [chapters_routes])
    client = make_client(app)
    auth_cookies(client, USER_ID)

    yield {"client": client, "factory": factory}
    engine.dispose()


def _create_chapter(client, *, number: int, title: str | None = None, plan: str | None = None, status: str = "planned") -> dict:
    payload: dict = {"number": number, "status": status}
    if title is not None:
        payload["title"] = title
    if plan is not None:
        payload["plan"] = plan
    resp = client.post(f"/api/projects/{PROJECT_ID}/chapters", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    return body["data"]["chapter"]


def test_list_chapter_meta_empty(env):
    """GET /chapters/meta：无章节时返回空页 + total=0。"""
    resp = env["client"].get(f"/api/projects/{PROJECT_ID}/chapters/meta")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["chapters"] == []
    assert data["total"] == 0
    assert data["returned"] == 0
    assert data["has_more"] is False


def test_create_chapter_returns_structure(env):
    """POST /chapters：创建单章，断言响应结构与默认状态。"""
    chapter = _create_chapter(env["client"], number=1, title="第一章", plan="开局")
    assert chapter["id"]
    assert chapter["project_id"] == PROJECT_ID
    assert chapter["outline_id"] == OUTLINE_ID
    assert chapter["number"] == 1
    assert chapter["title"] == "第一章"
    assert chapter["plan"] == "开局"
    assert chapter["status"] == "planned"
    assert "updated_at" in chapter


def test_get_chapter_detail(env):
    """GET /chapters/{id}：回读单章详情。"""
    created = _create_chapter(env["client"], number=1, title="详情章")
    resp = env["client"].get(f"/api/chapters/{created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    chapter = body["data"]["chapter"]
    assert chapter["id"] == created["id"]
    assert chapter["number"] == 1
    assert chapter["title"] == "详情章"


def test_list_chapters_sorted_by_number(env):
    """GET /chapters：按 number 升序返回。"""
    _create_chapter(env["client"], number=3, title="三")
    _create_chapter(env["client"], number=1, title="一")
    _create_chapter(env["client"], number=2, title="二")
    resp = env["client"].get(f"/api/projects/{PROJECT_ID}/chapters")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    chapters = body["data"]["chapters"]
    assert [c["number"] for c in chapters] == [1, 2, 3]


def test_list_chapter_meta_has_flags_and_pagination(env):
    """GET /chapters/meta：has_plan/has_content 标志 + limit 游标分页。"""
    _create_chapter(env["client"], number=1, title="一", plan="有大纲")  # has_plan=True
    _create_chapter(env["client"], number=2, title="二")  # has_plan=False
    _create_chapter(env["client"], number=3, title="三")

    resp = env["client"].get(f"/api/projects/{PROJECT_ID}/chapters/meta?limit=2")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 3
    assert data["returned"] == 2
    assert data["has_more"] is True
    assert data["next_cursor"] == 2
    # 第一页应为 number 1、2
    assert [c["number"] for c in data["chapters"]] == [1, 2]
    # has_plan 标志
    by_num = {c["number"]: c for c in data["chapters"]}
    assert by_num[1]["has_plan"] is True
    assert by_num[2]["has_plan"] is False
    assert by_num[1]["has_content"] is False

    # 翻第二页：cursor 之后
    resp2 = env["client"].get(f"/api/projects/{PROJECT_ID}/chapters/meta?limit=2&cursor={data['next_cursor']}")
    data2 = resp2.json()["data"]
    assert [c["number"] for c in data2["chapters"]] == [3]
    assert data2["has_more"] is False


def test_update_chapter_fields(env):
    """PUT /chapters/{id}：更新 title/plan/status 并回读验证。"""
    created = _create_chapter(env["client"], number=1, title="旧标题")
    resp = env["client"].put(
        f"/api/chapters/{created['id']}",
        json={"title": "新标题", "plan": "新计划", "status": "drafting"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    updated = body["data"]["chapter"]
    assert updated["title"] == "新标题"
    assert updated["plan"] == "新计划"
    assert updated["status"] == "drafting"

    # 回读确认持久化
    got = env["client"].get(f"/api/chapters/{created['id']}").json()["data"]["chapter"]
    assert got["title"] == "新标题"
    assert got["status"] == "drafting"


def test_delete_chapter(env):
    """DELETE /chapters/{id}：删除后 data 为空对象，再 GET 应 404。"""
    created = _create_chapter(env["client"], number=1, title="待删")
    resp = env["client"].delete(f"/api/chapters/{created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {}

    got = env["client"].get(f"/api/chapters/{created['id']}")
    assert got.status_code == 404
    assert got.json()["ok"] is False


def test_bulk_create_chapters(env):
    """POST /chapters/bulk_create：批量创建，返回按 number 升序、status=planned。"""
    resp = env["client"].post(
        f"/api/projects/{PROJECT_ID}/chapters/bulk_create",
        json={"chapters": [
            {"number": 2, "title": "二"},
            {"number": 1, "title": "一"},
            {"number": 3, "title": "三"},
        ]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    chapters = body["data"]["chapters"]
    assert [c["number"] for c in chapters] == [1, 2, 3]
    assert all(c["status"] == "planned" for c in chapters)
    assert all(c["outline_id"] == OUTLINE_ID for c in chapters)


def test_bulk_create_replace(env):
    """POST /chapters/bulk_create?replace=true：覆盖重建已有章节。"""
    _create_chapter(env["client"], number=1, title="旧一")
    resp = env["client"].post(
        f"/api/projects/{PROJECT_ID}/chapters/bulk_create?replace=true",
        json={"chapters": [
            {"number": 1, "title": "新一"},
            {"number": 2, "title": "新二"},
        ]},
    )
    assert resp.status_code == 200
    chapters = resp.json()["data"]["chapters"]
    assert {c["number"] for c in chapters} == {1, 2}
    titles = {c["number"]: c["title"] for c in chapters}
    assert titles[1] == "新一"
    assert titles[2] == "新二"

    # 列表只剩两章（旧 number=1 已被删除重建）
    listed = env["client"].get(f"/api/projects/{PROJECT_ID}/chapters").json()["data"]["chapters"]
    assert len(listed) == 2


def test_trigger_auto_updates_returns_tasks_and_token(env):
    """POST /chapters/{id}/trigger_auto_updates：返回 tasks 字典 + chapter_token。"""
    created = _create_chapter(env["client"], number=1, title="触发章")
    resp = env["client"].post(
        f"/api/chapters/{created['id']}/trigger_auto_updates",
        json={},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert "tasks" in data
    assert isinstance(data["tasks"], dict)
    assert "chapter_token" in data


def test_create_chapter_duplicate_number_conflicts(env):
    """POST /chapters：同 outline 下重复 number → 409 conflict（结构化 error）。"""
    _create_chapter(env["client"], number=1, title="一")
    resp = env["client"].post(
        f"/api/projects/{PROJECT_ID}/chapters",
        json={"number": 1, "title": "重复", "status": "planned"},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"]  # 断言存在错误码字段，不锁文案
