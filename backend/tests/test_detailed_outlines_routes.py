"""D 类 happy-path 测试：detailed_outlines 域路由。

断言【当前正确行为】，所有用例应绿（安全网）。覆盖细纲的 list/get/create/update/
delete 与 batch-create（无 LLM 调用）类端点。

跳过以下端点（不在本文件覆盖范围内）：
- POST .../detailed_outlines/generate（SSE 流式，真实调用 LLM）
- POST .../detailed_outlines/{volume_number}/generate（SSE 流式，真实调用 LLM）
- POST /detailed_outlines/{id}/create_chapters（其 happy-path 依赖私有服务
  ``create_chapters_from_detailed_outline`` 的章节字典 schema，超出路由契约，
  遵循"不深挖私有"原则不覆盖）
- POST /detailed_outlines/{id}/generate_chapters_stream（SSE 流式，真实调用 LLM）

项目归属：Project.owner_user_id="u1" 即通过 require_project_editor/viewer
的 "owner" 角色校验（见 app/api/deps.py:_project_role）。
"""

from __future__ import annotations

import pytest

from app.api.routes import detailed_outlines as detailed_outlines_routes
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


@pytest.fixture
def env():
    """每个用例独立的内存 DB + 已登录 client（owner=u1，含 project + outline）。"""
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    # 建全部已注册表，与同类 routes 测试保持一致（避免任何 fail-soft 副作用缺表）。
    create_tables(engine)
    seed_user(factory, user_id=USER_ID)

    with factory() as db:
        db.add(Project(id=PROJECT_ID, owner_user_id=USER_ID, name="测试项目", active_outline_id=OUTLINE_ID))
        db.add(Outline(id=OUTLINE_ID, project_id=PROJECT_ID, title="测试大纲", content_md=""))
        db.commit()

    app = make_test_app(factory, [detailed_outlines_routes])
    client = make_client(app)
    auth_cookies(client, USER_ID)

    yield {"client": client, "factory": factory}
    engine.dispose()


def _create_detailed_outline(
    client,
    *,
    volume_number: int,
    volume_title: str,
    content_md: str | None = None,
    structure: dict | None = None,
) -> dict:
    """通过 POST 创建一条细纲，返回 data.detailed_outline。"""
    payload: dict = {"volume_number": volume_number, "volume_title": volume_title}
    if content_md is not None:
        payload["content_md"] = content_md
    if structure is not None:
        payload["structure"] = structure
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/outlines/{OUTLINE_ID}/detailed_outlines",
        json=payload,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    return body["data"]["detailed_outline"]


# ---------- GET .../detailed_outlines（列表） ----------


def test_list_detailed_outlines_empty(env):
    """GET 列表：无细纲时返回空数组。"""
    resp = env["client"].get(f"/api/projects/{PROJECT_ID}/outlines/{OUTLINE_ID}/detailed_outlines")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["detailed_outlines"] == []


def test_list_detailed_outlines_returns_seeded_ordered(env):
    """GET 列表：返回 seeded 细纲，按 volume_number 升序。"""
    with env["factory"]() as db:
        db.add(DetailedOutline(
            id="d2", outline_id=OUTLINE_ID, project_id=PROJECT_ID,
            volume_number=2, volume_title="第二卷", status="planned",
        ))
        db.add(DetailedOutline(
            id="d1", outline_id=OUTLINE_ID, project_id=PROJECT_ID,
            volume_number=1, volume_title="第一卷", status="done",
        ))
        db.commit()

    resp = env["client"].get(f"/api/projects/{PROJECT_ID}/outlines/{OUTLINE_ID}/detailed_outlines")
    assert resp.status_code == 200
    items = resp.json()["data"]["detailed_outlines"]
    assert [it["volume_number"] for it in items] == [1, 2]
    first = items[0]
    assert first["id"] == "d1"
    assert first["outline_id"] == OUTLINE_ID
    assert first["volume_title"] == "第一卷"
    assert first["status"] == "done"
    assert first["chapter_count"] == 0
    assert "updated_at" in first


def test_list_detailed_outlines_chapter_count_from_structure(env):
    """GET 列表：chapter_count 由 structure_json 中 chapters 数组长度推导。"""
    with env["factory"]() as db:
        import json as _json
        db.add(DetailedOutline(
            id="d1", outline_id=OUTLINE_ID, project_id=PROJECT_ID,
            volume_number=1, volume_title="第一卷", status="done",
            structure_json=_json.dumps({"chapters": [{"n": 1}, {"n": 2}, {"n": 3}]}),
        ))
        db.commit()

    items = env["client"].get(
        f"/api/projects/{PROJECT_ID}/outlines/{OUTLINE_ID}/detailed_outlines"
    ).json()["data"]["detailed_outlines"]
    assert items[0]["chapter_count"] == 3


# ---------- POST .../detailed_outlines（创建） ----------


def test_create_detailed_outline_basic(env):
    """POST 创建：返回 2xx + 关键键，默认 status=planned。"""
    created = _create_detailed_outline(
        env["client"], volume_number=1, volume_title="第一卷", content_md="# 卷一\n开局",
    )
    assert created["id"]
    assert created["outline_id"] == OUTLINE_ID
    assert created["project_id"] == PROJECT_ID
    assert created["volume_number"] == 1
    assert created["volume_title"] == "第一卷"
    assert created["content_md"] == "# 卷一\n开局"
    assert created["status"] == "planned"
    assert created["structure"] is None
    assert "created_at" in created
    assert "updated_at" in created

    # 回读确认持久化
    got = env["client"].get(f"/api/detailed_outlines/{created['id']}").json()["data"]["detailed_outline"]
    assert got["id"] == created["id"]
    assert got["volume_title"] == "第一卷"


def test_create_detailed_outline_with_structure(env):
    """POST 创建：带 structure 时回读为解析后的对象。"""
    structure = {"chapters": [{"number": 1, "title": "第一章", "summary": "..."}]}
    created = _create_detailed_outline(
        env["client"], volume_number=1, volume_title="第一卷", structure=structure,
    )
    assert created["structure"] is not None
    assert created["structure"]["chapters"][0]["title"] == "第一章"

    # 列表里 chapter_count 应反映结构中的章节数
    items = env["client"].get(
        f"/api/projects/{PROJECT_ID}/outlines/{OUTLINE_ID}/detailed_outlines"
    ).json()["data"]["detailed_outlines"]
    assert items[0]["chapter_count"] == 1


# ---------- GET /detailed_outlines/{id}（详情） ----------


def test_get_detailed_outline_returns_structure(env):
    """GET 单条：返回细纲详情，含关键键。"""
    created = _create_detailed_outline(env["client"], volume_number=1, volume_title="第一卷")

    resp = env["client"].get(f"/api/detailed_outlines/{created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    detail = body["data"]["detailed_outline"]
    assert detail["id"] == created["id"]
    assert detail["outline_id"] == OUTLINE_ID
    assert detail["project_id"] == PROJECT_ID
    assert detail["volume_number"] == 1
    assert detail["volume_title"] == "第一卷"


# ---------- PUT /detailed_outlines/{id}（编辑） ----------


def test_update_detailed_outline_title_and_content(env):
    """PUT：更新 volume_title + content_md 并回读。"""
    created = _create_detailed_outline(env["client"], volume_number=1, volume_title="旧标题")

    resp = env["client"].put(
        f"/api/detailed_outlines/{created['id']}",
        json={"volume_title": "新标题", "content_md": "# 改后\n..."},
    )
    assert resp.status_code == 200
    outline = resp.json()["data"]["detailed_outline"]
    assert outline["volume_title"] == "新标题"
    assert outline["content_md"] == "# 改后\n..."

    got = env["client"].get(f"/api/detailed_outlines/{created['id']}").json()["data"]["detailed_outline"]
    assert got["volume_title"] == "新标题"
    assert got["content_md"] == "# 改后\n..."


def test_update_detailed_outline_structure_and_status(env):
    """PUT：更新 structure + status 并回读。"""
    created = _create_detailed_outline(env["client"], volume_number=1, volume_title="第一卷")

    structure = {"chapters": [{"number": 1, "title": "唯一章"}]}
    resp = env["client"].put(
        f"/api/detailed_outlines/{created['id']}",
        json={"structure": structure, "status": "done"},
    )
    assert resp.status_code == 200
    outline = resp.json()["data"]["detailed_outline"]
    assert outline["structure"] is not None
    assert outline["structure"]["chapters"][0]["title"] == "唯一章"
    assert outline["status"] == "done"


# ---------- DELETE /detailed_outlines/{id}（删除） ----------


def test_delete_detailed_outline(env):
    """DELETE：返回 deleted=True，再 GET 应 404。"""
    created = _create_detailed_outline(env["client"], volume_number=1, volume_title="待删")

    resp = env["client"].delete(f"/api/detailed_outlines/{created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["deleted"] is True

    got = env["client"].get(f"/api/detailed_outlines/{created['id']}")
    assert got.status_code == 404
    assert got.json()["ok"] is False


# ---------- POST .../detailed_outlines/batch（批量创建/upsert，无 LLM） ----------


def test_batch_create_detailed_outlines_new(env):
    """POST batch：从预解析数据新建多条，返回 count + ids，并回读。"""
    resp = env["client"].post(
        f"/api/projects/{PROJECT_ID}/outlines/{OUTLINE_ID}/detailed_outlines/batch",
        json={
            "detailed_outlines": [
                {
                    "volume_number": 1,
                    "volume_title": "第一卷",
                    "volume_summary": "卷一总览",
                    "chapters": [
                        {"number": 1, "title": "第一章", "summary": "开篇"},
                        {"number": 2, "title": "第二章", "summary": "发展"},
                    ],
                },
                {
                    "volume_number": 2,
                    "volume_title": "第二卷",
                    "volume_summary": "",
                    "chapters": [],
                },
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["count"] == 2
    assert len(body["data"]["ids"]) == 2

    # 回读：列表按卷号升序，状态 done，chapter_count 反映结构
    items = env["client"].get(
        f"/api/projects/{PROJECT_ID}/outlines/{OUTLINE_ID}/detailed_outlines"
    ).json()["data"]["detailed_outlines"]
    assert [it["volume_number"] for it in items] == [1, 2]
    by_vol = {it["volume_number"]: it for it in items}
    assert by_vol[1]["status"] == "done"
    assert by_vol[1]["chapter_count"] == 2
    assert by_vol[2]["chapter_count"] == 0


def test_batch_create_detailed_outlines_upsert_existing(env):
    """POST batch：对同 outline+volume_number 已存在的细纲执行 upsert（覆盖）。"""
    # 先批量建一卷
    env["client"].post(
        f"/api/projects/{PROJECT_ID}/outlines/{OUTLINE_ID}/detailed_outlines/batch",
        json={
            "detailed_outlines": [
                {"volume_number": 1, "volume_title": "原名", "volume_summary": "原摘要", "chapters": []},
            ]
        },
    )
    first_ids = env["client"].get(
        f"/api/projects/{PROJECT_ID}/outlines/{OUTLINE_ID}/detailed_outlines"
    ).json()["data"]["detailed_outlines"]
    assert len(first_ids) == 1
    existing_id = first_ids[0]["id"]

    # 再次批量提交相同卷号 → upsert 覆盖，不新增行
    resp = env["client"].post(
        f"/api/projects/{PROJECT_ID}/outlines/{OUTLINE_ID}/detailed_outlines/batch",
        json={
            "detailed_outlines": [
                {
                    "volume_number": 1,
                    "volume_title": "新名",
                    "volume_summary": "新摘要",
                    "chapters": [{"number": 1, "title": "第一章", "summary": "x"}],
                },
            ]
        },
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["count"] == 1

    items = env["client"].get(
        f"/api/projects/{PROJECT_ID}/outlines/{OUTLINE_ID}/detailed_outlines"
    ).json()["data"]["detailed_outlines"]
    assert len(items) == 1  # upsert 未新增
    assert items[0]["id"] == existing_id
    assert items[0]["volume_title"] == "新名"
    assert items[0]["chapter_count"] == 1
