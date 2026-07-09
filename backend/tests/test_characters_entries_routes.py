"""D 类 happy-path 测试：characters 与 entries 域路由。

断言【当前正确行为】，所有用例应绿（安全网）。仅覆盖主要 CRUD/读取/排序类
端点；两域不含 LLM/生成类端点，故全部 CRUD 都在本文件覆盖范围内。

项目归属：Project.owner_user_id="u1" 即通过 require_project_editor/viewer
的 "owner" 角色校验（见 app/api/deps.py:_project_role）。

characters 域的 create/update/delete 会 fail-soft 调 schedule_search_rebuild_task
写任务相关表，故 env 用 create_tables(engine) 建全部已注册表，避免缺表报错。
entries 域为纯 CRUD（无副作用写其它表）。

覆盖端点：
  GET    /api/projects/{project_id}/characters
  POST   /api/projects/{project_id}/characters
  PUT    /api/characters/{character_id}
  DELETE /api/characters/{character_id}
  GET    /api/projects/{project_id}/entries        (含 tag 过滤、分页)
  POST   /api/projects/{project_id}/entries
  PUT    /api/entries/{entry_id}
  DELETE /api/entries/{entry_id}
"""

from __future__ import annotations

import pytest

from app.api.routes import characters as characters_routes
from app.api.routes import entries as entries_routes
from app.models.character import Character
from app.models.entry import Entry
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
USER_ID = "u1"


@pytest.fixture
def env():
    """每个用例独立的内存 DB + 已登录 client（owner=u1，含一个项目）。"""
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    # characters 写路径调 schedule_search_rebuild_task（fail-soft 写多表），
    # 故建全部已注册表，避免缺表报错。
    create_tables(engine)
    seed_user(factory, user_id=USER_ID)

    with factory() as db:
        db.add(Project(id=PROJECT_ID, owner_user_id=USER_ID, name="测试项目"))
        db.commit()

    app = make_test_app(factory, [characters_routes, entries_routes])
    client = make_client(app)
    auth_cookies(client, USER_ID)

    yield {"client": client, "factory": factory}
    engine.dispose()


# ============================ characters ============================


def _create_character(
    client,
    *,
    name: str = "主角",
    role: str | None = "protagonist",
    profile: str | None = "勇敢善良",
    notes: str | None = None,
) -> dict:
    payload: dict = {"name": name}
    if role is not None:
        payload["role"] = role
    if profile is not None:
        payload["profile"] = profile
    if notes is not None:
        payload["notes"] = notes
    resp = client.post(f"/api/projects/{PROJECT_ID}/characters", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    return body["data"]["character"]


def test_list_characters_empty(env):
    """GET /characters：无角色时返回空列表。"""
    resp = env["client"].get(f"/api/projects/{PROJECT_ID}/characters")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {"characters": []}


def test_create_character_returns_structure_and_persists(env):
    """POST /characters：创建角色，断言响应结构 + 持久化回读。"""
    char = _create_character(env["client"], name="林冲", role="主角", profile="豹子头", notes="八十万禁军教头")
    assert char["id"]
    assert char["project_id"] == PROJECT_ID
    assert char["name"] == "林冲"
    assert char["role"] == "主角"
    assert char["profile"] == "豹子头"
    assert char["notes"] == "八十万禁军教头"
    assert "updated_at" in char

    # 回读持久化
    with env["factory"]() as db:
        row = db.get(Character, char["id"])
        assert row is not None
        assert row.name == "林冲"
        assert row.role == "主角"


def test_list_characters_returns_created(env):
    """GET /characters：列出已创建的角色（断言集合而非顺序，时间戳同秒不稳定）。"""
    c1 = _create_character(env["client"], name="甲")
    c2 = _create_character(env["client"], name="乙")
    resp = env["client"].get(f"/api/projects/{PROJECT_ID}/characters")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    chars = body["data"]["characters"]
    assert len(chars) == 2
    assert {c["id"] for c in chars} == {c1["id"], c2["id"]}
    assert {c["name"] for c in chars} == {"甲", "乙"}


def test_update_character_fields(env):
    """PUT /characters/{id}：更新 name/role/profile/notes 并回读验证。"""
    created = _create_character(env["client"], name="旧名", role="旧角色")
    resp = env["client"].put(
        f"/api/characters/{created['id']}",
        json={"name": "新名", "role": "新角色", "profile": "新档案", "notes": "新备注"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    updated = body["data"]["character"]
    assert updated["id"] == created["id"]
    assert updated["name"] == "新名"
    assert updated["role"] == "新角色"
    assert updated["profile"] == "新档案"
    assert updated["notes"] == "新备注"

    # 回读确认持久化
    got = env["client"].get(f"/api/projects/{PROJECT_ID}/characters").json()["data"]["characters"]
    by_id = {c["id"]: c for c in got}
    assert by_id[created["id"]]["name"] == "新名"
    assert by_id[created["id"]]["role"] == "新角色"


def test_delete_character(env):
    """DELETE /characters/{id}：删除后 data 为空对象，列表中不再出现。"""
    created = _create_character(env["client"], name="待删")
    resp = env["client"].delete(f"/api/characters/{created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {}

    listed = env["client"].get(f"/api/projects/{PROJECT_ID}/characters").json()["data"]["characters"]
    assert created["id"] not in {c["id"] for c in listed}

    # 回读确认 DB 已删除
    with env["factory"]() as db:
        assert db.get(Character, created["id"]) is None


# ============================ entries ============================


def _create_entry(
    client,
    *,
    title: str = "一条设定",
    content: str = "正文内容",
    tags: list[str] | None = None,
) -> dict:
    payload: dict = {"title": title, "content": content}
    if tags is not None:
        payload["tags"] = tags
    resp = client.post(f"/api/projects/{PROJECT_ID}/entries", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    return body["data"]["entry"]


def test_list_entries_empty(env):
    """GET /entries：无条目时返回空 items + next_offset=None。"""
    resp = env["client"].get(f"/api/projects/{PROJECT_ID}/entries")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {"items": [], "next_offset": None}


def test_create_entry_returns_structure_and_persists(env):
    """POST /entries：创建条目，断言响应结构（含 tags 归一化）+ 持久化。"""
    entry = _create_entry(
        env["client"],
        title="  世界观设定  ",  # title 会被 strip
        content="魔法体系说明",
        tags=["设定", " 伏笔 ", "设定", "情节"],  # 去空白、去重（大小写不敏感）
    )
    assert entry["id"]
    assert entry["project_id"] == PROJECT_ID
    assert entry["title"] == "世界观设定"  # strip 归一化
    assert entry["content"] == "魔法体系说明"
    # tags 去空白 + 去重，顺序保留
    assert entry["tags"] == ["设定", "伏笔", "情节"]
    assert "created_at" in entry
    assert "updated_at" in entry

    # 回读持久化
    with env["factory"]() as db:
        row = db.get(Entry, entry["id"])
        assert row is not None
        assert row.title == "世界观设定"


def test_list_entries_returns_created(env):
    """GET /entries：列出已创建条目。"""
    e1 = _create_entry(env["client"], title="一")
    e2 = _create_entry(env["client"], title="二")
    resp = env["client"].get(f"/api/projects/{PROJECT_ID}/entries")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    items = body["data"]["items"]
    assert len(items) == 2
    assert {it["id"] for it in items} == {e1["id"], e2["id"]}
    assert {it["title"] for it in items} == {"一", "二"}


def test_list_entries_tag_filter(env):
    """GET /entries?tag=：按 tag 过滤（tag 存于 tags_json，模糊匹配）。"""
    _create_entry(env["client"], title="设定类", tags=["设定"])
    _create_entry(env["client"], title="伏笔类", tags=["伏笔"])
    _create_entry(env["client"], title="设定与情节", tags=["设定", "情节"])

    resp = env["client"].get(f"/api/projects/{PROJECT_ID}/entries?tag=设定")
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    titles = {it["title"] for it in items}
    assert titles == {"设定类", "设定与情节"}


def test_list_entries_pagination(env):
    """GET /entries?limit=：条目数超过 limit 时返回 next_offset 游标。"""
    for i in range(3):
        _create_entry(env["client"], title=f"条目{i}")

    resp = env["client"].get(f"/api/projects/{PROJECT_ID}/entries?limit=2&offset=0")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["items"]) == 2
    assert data["next_offset"] == 2

    # 翻第二页
    resp2 = env["client"].get(f"/api/projects/{PROJECT_ID}/entries?limit=2&offset={data['next_offset']}")
    data2 = resp2.json()["data"]
    assert len(data2["items"]) == 1
    assert data2["next_offset"] is None


def test_update_entry_fields(env):
    """PUT /entries/{id}：更新 title/content/tags 并回读验证。"""
    created = _create_entry(env["client"], title="旧标题", content="旧正文")
    resp = env["client"].put(
        f"/api/entries/{created['id']}",
        json={"title": "新标题", "content": "新正文", "tags": ["修改", "设定"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    updated = body["data"]["entry"]
    assert updated["id"] == created["id"]
    assert updated["title"] == "新标题"
    assert updated["content"] == "新正文"
    assert updated["tags"] == ["修改", "设定"]

    # 回读确认持久化
    got = env["client"].get(f"/api/projects/{PROJECT_ID}/entries").json()["data"]["items"]
    by_id = {it["id"]: it for it in got}
    assert by_id[created["id"]]["title"] == "新标题"
    assert by_id[created["id"]]["tags"] == ["修改", "设定"]


def test_delete_entry(env):
    """DELETE /entries/{id}：删除后 data 为空对象，列表中不再出现。"""
    created = _create_entry(env["client"], title="待删")
    resp = env["client"].delete(f"/api/entries/{created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {}

    listed = env["client"].get(f"/api/projects/{PROJECT_ID}/entries").json()["data"]["items"]
    assert created["id"] not in {it["id"] for it in listed}

    # 回读确认 DB 已删除
    with env["factory"]() as db:
        assert db.get(Entry, created["id"]) is None
