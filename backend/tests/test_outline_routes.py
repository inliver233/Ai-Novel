"""D 类 happy-path 测试：outline 域路由。

断言【当前正确行为】，所有用例应绿（安全网）。覆盖大纲读取 / 编辑 / CRUD 类
端点；依赖外部 LLM 的生成 / 解析 / 流式端点（outline/generate、
outline/generate-stream、outline/parse、outline/parse-stream）不在本文件覆盖
范围内——这些端点会真实调用 LLM，不 mock。

项目归属：Project.owner_user_id="u1" 即通过 require_project_editor/viewer
的 "owner" 角色校验（见 app/api/deps.py:_project_role）。
"""

from __future__ import annotations

import pytest

from app.api.routes import outline as outline_routes
from app.api.routes import outlines as outlines_routes
from app.models.chapter import Chapter
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
    # outline / outlines 的 create/update/delete 会 fail-soft 地写
    # ProjectSettings / ProjectTask 等表（schedule_vector_rebuild_task /
    # schedule_search_rebuild_task），故建全部已注册表，避免缺表报错。
    create_tables(engine)
    seed_user(factory, user_id=USER_ID)

    with factory() as db:
        db.add(Project(id=PROJECT_ID, owner_user_id=USER_ID, name="测试项目", active_outline_id=OUTLINE_ID))
        db.add(Outline(id=OUTLINE_ID, project_id=PROJECT_ID, title="测试大纲", content_md=""))
        db.commit()

    app = make_test_app(factory, [outline_routes, outlines_routes])
    client = make_client(app)
    auth_cookies(client, USER_ID)

    yield {"client": client, "factory": factory}
    engine.dispose()


# ---------- GET /projects/{project_id}/outline（活跃大纲读取） ----------


def test_get_active_outline_returns_structure(env):
    """GET /projects/{p1}/outline：返回活跃大纲，含关键键。"""
    resp = env["client"].get(f"/api/projects/{PROJECT_ID}/outline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    outline = body["data"]["outline"]
    assert outline["id"] == OUTLINE_ID
    assert outline["project_id"] == PROJECT_ID
    assert outline["title"] == "测试大纲"
    assert "content_md" in outline
    assert "structure" in outline
    assert "updated_at" in outline


def test_get_outline_without_any_row_is_read_only_transient_default(env):
    """GET 无大纲时返回瞬态默认载荷（id=""），绝不落库（backend-api#5）。"""
    with env["factory"]() as db:
        project = db.get(Project, PROJECT_ID)
        project.active_outline_id = None
        outline = db.get(Outline, OUTLINE_ID)
        db.delete(outline)
        db.commit()

    resp = env["client"].get(f"/api/projects/{PROJECT_ID}/outline")
    assert resp.status_code == 200
    outline_payload = resp.json()["data"]["outline"]
    assert outline_payload["id"] == ""
    assert outline_payload["content_md"] == ""

    with env["factory"]() as db:
        assert db.query(Outline).count() == 0
        project = db.get(Project, PROJECT_ID)
        assert project.active_outline_id is None


# ---------- PUT /projects/{project_id}/outline（活跃大纲编辑） ----------


def test_put_active_outline_updates_title(env):
    """PUT /projects/{p1}/outline：更新 title 并回读验证。"""
    resp = env["client"].put(
        f"/api/projects/{PROJECT_ID}/outline",
        json={"title": "新大纲标题"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    outline = body["data"]["outline"]
    assert outline["id"] == OUTLINE_ID
    assert outline["title"] == "新大纲标题"

    # 回读确认持久化
    got = env["client"].get(f"/api/projects/{PROJECT_ID}/outline").json()["data"]["outline"]
    assert got["title"] == "新大纲标题"


def test_put_active_outline_updates_content_and_structure(env):
    """PUT /projects/{p1}/outline：同时更新 content_md 与 structure。"""
    structure = {"volumes": [{"number": 1, "title": "第一卷", "beats": []}]}
    resp = env["client"].put(
        f"/api/projects/{PROJECT_ID}/outline",
        json={"content_md": "# 大纲\n第一章 ...", "structure": structure},
    )
    assert resp.status_code == 200
    outline = resp.json()["data"]["outline"]
    assert outline["content_md"] == "# 大纲\n第一章 ..."
    # 断言结构化字段键名，不锁具体序列化
    assert outline["structure"] is not None
    assert outline["structure"]["volumes"][0]["title"] == "第一卷"


# ---------- GET /projects/{project_id}/outlines（列表） ----------


def test_list_outlines_returns_seeded(env):
    """GET /projects/{p1}/outlines：返回 seeded 大纲，has_chapters=False。"""
    resp = env["client"].get(f"/api/projects/{PROJECT_ID}/outlines")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    items = body["data"]["outlines"]
    assert len(items) == 1
    item = items[0]
    assert item["id"] == OUTLINE_ID
    assert item["title"] == "测试大纲"
    assert item["has_chapters"] is False
    assert "updated_at" in item
    assert "created_at" in item


def test_list_outlines_has_chapters_flag(env):
    """GET /projects/{p1}/outlines：outline 下有章节时 has_chapters=True。"""
    with env["factory"]() as db:
        db.add(Chapter(id="c1", project_id=PROJECT_ID, outline_id=OUTLINE_ID, number=1, title="第一章"))
        db.commit()
    resp = env["client"].get(f"/api/projects/{PROJECT_ID}/outlines")
    items = resp.json()["data"]["outlines"]
    by_id = {it["id"]: it for it in items}
    assert by_id[OUTLINE_ID]["has_chapters"] is True


# ---------- POST /projects/{project_id}/outlines（创建） ----------


def test_create_outline_sets_active(env):
    """POST /projects/{p1}/outlines：创建新大纲并自动设为 active。"""
    resp = env["client"].post(
        f"/api/projects/{PROJECT_ID}/outlines",
        json={"title": "新建大纲", "content_md": "# 新\n内容"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    created = body["data"]["outline"]
    assert created["id"]
    assert created["project_id"] == PROJECT_ID
    assert created["title"] == "新建大纲"
    assert created["content_md"] == "# 新\n内容"

    # 新大纲应已成为活跃大纲
    active = env["client"].get(f"/api/projects/{PROJECT_ID}/outline").json()["data"]["outline"]
    assert active["id"] == created["id"]


# ---------- GET /projects/{project_id}/outlines/{outline_id}（详情） ----------


def test_get_outline_detail(env):
    """GET /projects/{p1}/outlines/{id}：回读单条大纲详情。"""
    resp = env["client"].get(f"/api/projects/{PROJECT_ID}/outlines/{OUTLINE_ID}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    outline = body["data"]["outline"]
    assert outline["id"] == OUTLINE_ID
    assert outline["title"] == "测试大纲"


# ---------- PUT /projects/{project_id}/outlines/{outline_id}（编辑） ----------


def test_update_outline_item_title_and_content(env):
    """PUT /projects/{p1}/outlines/{id}：更新 title + content_md 并回读。"""
    resp = env["client"].put(
        f"/api/projects/{PROJECT_ID}/outlines/{OUTLINE_ID}",
        json={"title": "改后标题", "content_md": "# 改后\n..."},
    )
    assert resp.status_code == 200
    outline = resp.json()["data"]["outline"]
    assert outline["title"] == "改后标题"
    assert outline["content_md"] == "# 改后\n..."

    got = env["client"].get(f"/api/projects/{PROJECT_ID}/outlines/{OUTLINE_ID}").json()["data"]["outline"]
    assert got["title"] == "改后标题"


def test_update_outline_item_structure_only(env):
    """PUT /projects/{p1}/outlines/{id}：仅传 structure（不带 content_md）也能持久化。"""
    structure = {"chapters": [{"number": 1, "title": "唯一章", "beats": []}]}
    resp = env["client"].put(
        f"/api/projects/{PROJECT_ID}/outlines/{OUTLINE_ID}",
        json={"structure": structure},
    )
    assert resp.status_code == 200
    outline = resp.json()["data"]["outline"]
    assert outline["structure"] is not None
    assert outline["structure"]["chapters"][0]["title"] == "唯一章"


# ---------- DELETE /projects/{project_id}/outlines/{outline_id}（删除） ----------


def test_delete_outline_item(env):
    """DELETE /projects/{p1}/outlines/{id}：删除后 data={}，再 GET 应 404。"""
    # 先创建一条非活跃大纲用于删除，避免删掉唯一的 active
    created = env["client"].post(
        f"/api/projects/{PROJECT_ID}/outlines",
        json={"title": "待删"},
    ).json()["data"]["outline"]
    new_id = created["id"]

    resp = env["client"].delete(f"/api/projects/{PROJECT_ID}/outlines/{new_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {}

    got = env["client"].get(f"/api/projects/{PROJECT_ID}/outlines/{new_id}")
    assert got.status_code == 404
    assert got.json()["ok"] is False


def test_delete_active_outline_switches_active(env):
    """DELETE active 大纲后，active_outline_id 切到剩余最新的一条。"""
    # seed 一条额外大纲；OUTLINE_ID 是当前 active。o_old 在测试体里创建，
    # 其 updated_at 默认值晚于 fixture seed 的 o1，删除 o1 后仅剩 o_old。
    with env["factory"]() as db:
        db.add(Outline(id="o_old", project_id=PROJECT_ID, title="旧大纲", content_md=""))
        db.commit()

    resp = env["client"].delete(f"/api/projects/{PROJECT_ID}/outlines/{OUTLINE_ID}")
    assert resp.status_code == 200

    # 删除后 active 应切到唯一剩余的大纲 o_old
    active = env["client"].get(f"/api/projects/{PROJECT_ID}/outline").json()["data"]["outline"]
    assert active["id"] == "o_old"
