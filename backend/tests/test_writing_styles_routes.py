"""writing_styles 域路由 happy-path（D 类安全网）。

覆盖 ``app/api/routes/writing_styles.py`` 的主要 HTTP 端点：

  GET    /api/writing_styles/presets
  GET    /api/writing_styles
  POST   /api/writing_styles
  PUT    /api/writing_styles/{style_id}
  DELETE /api/writing_styles/{style_id}
  GET    /api/projects/{project_id}/writing_style_default
  PUT    /api/projects/{project_id}/writing_style_default

这些都是当前正确的活功能，测试应保持绿，构成 writing_styles 域的安全网。

跳过项：无。本路由全部为纯 CRUD，无外部 LLM/向量/复杂状态依赖。
"""

from __future__ import annotations

from app.api.routes import writing_styles as writing_styles_routes
from app.models.project import Project
from app.models.project_default_style import ProjectDefaultStyle
from app.models.project_membership import ProjectMembership
from app.models.user import User
from app.models.user_password import UserPassword
from app.models.writing_style import WritingStyle

from tests.support import (
    auth_cookies,
    create_tables,
    make_client,
    make_session_factory,
    make_sqlite_engine,
    make_test_app,
    seed_user,
)


# 所有测试需要建的表：WritingStyle / ProjectDefaultStyle 自身，加上 user 登录态
# （User/UserPassword）以及项目默认风格路径（Project/ProjectMembership）。
_MODELS = [
    User,
    UserPassword,
    Project,
    ProjectMembership,
    WritingStyle,
    ProjectDefaultStyle,
]


def _new_client_and_factory():
    """构造一个已建表 + 已登录 u1 的测试 client 与 session factory。"""
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, _MODELS)
    seed_user(factory, user_id="u1")
    app = make_test_app(factory, [writing_styles_routes])
    client = make_client(app)
    auth_cookies(client, "u1")
    return client, factory


def _seed_project(factory, *, project_id: str = "p1", owner_user_id: str = "u1") -> str:
    """直接在 DB 插入一个 Project + owner membership，返回 project_id。"""
    with factory() as db:
        db.add(Project(id=project_id, owner_user_id=owner_user_id, name="P1"))
        db.add(ProjectMembership(project_id=project_id, user_id=owner_user_id, role="owner"))
        db.commit()
    return project_id


def _seed_preset_style(
    factory,
    *,
    style_id: str = "preset-1",
    name: str = "Preset Style",
    prompt_content: str = "preset prompt",
) -> str:
    """直接在 DB 插入一个 preset 风格，返回 style_id。"""
    with factory() as db:
        db.add(
            WritingStyle(
                id=style_id,
                owner_user_id=None,
                name=name,
                description=None,
                prompt_content=prompt_content,
                is_preset=True,
            )
        )
        db.commit()
    return style_id


# ---------- GET /api/writing_styles/presets ----------


def test_list_presets_empty_returns_ok_with_empty_list() -> None:
    client, _factory = _new_client_and_factory()
    resp = client.get("/api/writing_styles/presets")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {"styles": []}


def test_list_presets_returns_seeded_preset() -> None:
    client, factory = _new_client_and_factory()
    _seed_preset_style(factory, style_id="preset-1", name="Preset A")
    resp = client.get("/api/writing_styles/presets")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    styles = body["data"]["styles"]
    assert len(styles) == 1
    s = styles[0]
    assert s["id"] == "preset-1"
    assert s["name"] == "Preset A"
    assert s["is_preset"] is True


# ---------- GET /api/writing_styles ----------


def test_list_user_styles_empty_returns_ok_with_empty_list() -> None:
    client, _factory = _new_client_and_factory()
    resp = client.get("/api/writing_styles")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {"styles": []}


def test_list_user_styles_returns_created_style() -> None:
    client, _factory = _new_client_and_factory()
    create_resp = client.post(
        "/api/writing_styles",
        json={"name": "My Style", "prompt_content": "write softly"},
    )
    assert create_resp.status_code == 200
    created_id = create_resp.json()["data"]["style"]["id"]

    resp = client.get("/api/writing_styles")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    styles = body["data"]["styles"]
    assert len(styles) == 1
    assert styles[0]["id"] == created_id
    assert styles[0]["owner_user_id"] == "u1"
    assert styles[0]["is_preset"] is False


# ---------- POST /api/writing_styles ----------


def test_create_style_returns_style_and_persists() -> None:
    client, factory = _new_client_and_factory()
    resp = client.post(
        "/api/writing_styles",
        json={
            "name": "Created Style",
            "description": "a description",
            "prompt_content": "write formally",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    style = body["data"]["style"]
    assert style["id"]  # new_id 生成的 UUID
    assert style["name"] == "Created Style"
    assert style["description"] == "a description"
    assert style["prompt_content"] == "write formally"
    assert style["owner_user_id"] == "u1"
    assert style["is_preset"] is False

    # 回读验证持久化
    with factory() as db:
        row = db.get(WritingStyle, style["id"])
        assert row is not None
        assert row.name == "Created Style"
        assert row.is_preset is False


# ---------- PUT /api/writing_styles/{style_id} ----------


def test_update_style_basic_fields() -> None:
    client, _factory = _new_client_and_factory()
    create_resp = client.post(
        "/api/writing_styles",
        json={"name": "Old", "prompt_content": "old prompt"},
    )
    assert create_resp.status_code == 200
    style_id = create_resp.json()["data"]["style"]["id"]

    resp = client.put(
        f"/api/writing_styles/{style_id}",
        json={"name": "New Name", "description": "new desc", "prompt_content": "new prompt"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    style = body["data"]["style"]
    assert style["id"] == style_id
    assert style["name"] == "New Name"
    assert style["description"] == "new desc"
    assert style["prompt_content"] == "new prompt"


def test_update_style_partial_only_name_changes() -> None:
    client, _factory = _new_client_and_factory()
    create_resp = client.post(
        "/api/writing_styles",
        json={"name": "Old", "description": "keep me", "prompt_content": "keep prompt"},
    )
    assert create_resp.status_code == 200
    style_id = create_resp.json()["data"]["style"]["id"]

    resp = client.put(
        f"/api/writing_styles/{style_id}",
        json={"name": "New Name Only"},
    )
    assert resp.status_code == 200
    body = resp.json()
    style = body["data"]["style"]
    assert style["name"] == "New Name Only"
    # 未传字段保持原值
    assert style["prompt_content"] == "keep prompt"


# ---------- DELETE /api/writing_styles/{style_id} ----------


def test_delete_style_returns_ok_and_removes_style() -> None:
    client, factory = _new_client_and_factory()
    create_resp = client.post(
        "/api/writing_styles",
        json={"name": "ToDelete", "prompt_content": "p"},
    )
    assert create_resp.status_code == 200
    style_id = create_resp.json()["data"]["style"]["id"]

    resp = client.delete(f"/api/writing_styles/{style_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {}

    # 回读验证已删除
    with factory() as db:
        assert db.get(WritingStyle, style_id) is None


def test_delete_style_clears_project_default_reference() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")

    create_resp = client.post(
        "/api/writing_styles",
        json={"name": "Default", "prompt_content": "p"},
    )
    assert create_resp.status_code == 200
    style_id = create_resp.json()["data"]["style"]["id"]

    # 设为项目默认风格
    put_resp = client.put(
        "/api/projects/p1/writing_style_default",
        json={"style_id": style_id},
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["data"]["default"]["style_id"] == style_id

    # 删除风格后，项目默认引用应被置空（SET NULL 行为由路由显式 update 实现）
    del_resp = client.delete(f"/api/writing_styles/{style_id}")
    assert del_resp.status_code == 200

    get_resp = client.get("/api/projects/p1/writing_style_default")
    assert get_resp.status_code == 200
    assert get_resp.json()["data"]["default"]["style_id"] is None


# ---------- GET /api/projects/{project_id}/writing_style_default ----------


def test_get_project_default_style_returns_none_when_unset() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    resp = client.get("/api/projects/p1/writing_style_default")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    default = body["data"]["default"]
    assert default["project_id"] == "p1"
    assert default["style_id"] is None


# ---------- PUT /api/projects/{project_id}/writing_style_default ----------


def test_put_project_default_style_sets_user_style() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")

    create_resp = client.post(
        "/api/writing_styles",
        json={"name": "Default", "prompt_content": "p"},
    )
    assert create_resp.status_code == 200
    style_id = create_resp.json()["data"]["style"]["id"]

    resp = client.put(
        "/api/projects/p1/writing_style_default",
        json={"style_id": style_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    default = body["data"]["default"]
    assert default["project_id"] == "p1"
    assert default["style_id"] == style_id

    # 回读验证持久化
    with factory() as db:
        row = db.get(ProjectDefaultStyle, "p1")
        assert row is not None
        assert row.style_id == style_id


def test_put_project_default_style_can_clear_to_none() -> None:
    client, _factory = _new_client_and_factory()
    _seed_project(_factory, project_id="p1")

    create_resp = client.post(
        "/api/writing_styles",
        json={"name": "Default", "prompt_content": "p"},
    )
    style_id = create_resp.json()["data"]["style"]["id"]

    client.put("/api/projects/p1/writing_style_default", json={"style_id": style_id})

    # 清空默认
    resp = client.put(
        "/api/projects/p1/writing_style_default",
        json={"style_id": None},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["default"]["style_id"] is None


def test_put_project_default_style_accepts_preset_style() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    _seed_preset_style(factory, style_id="preset-1", name="Preset")

    resp = client.put(
        "/api/projects/p1/writing_style_default",
        json={"style_id": "preset-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["default"]["style_id"] == "preset-1"
