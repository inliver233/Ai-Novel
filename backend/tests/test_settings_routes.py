"""settings 域路由 happy-path（D 类安全网）。

覆盖 ``app/api/routes/settings.py`` 的 HTTP 端点：

  GET    /api/projects/{project_id}/settings
  PUT    /api/projects/{project_id}/settings

这些是当前正确的活功能，测试应保持绿，构成 settings 域的安全网。

settings 是项目级（非全局），需先建 Project + owner membership。GET 要求
viewer 角色、PUT 要求 editor 角色，project owner 同时满足两者。
``_build_settings_payload`` 在 ProjectSettings 行缺失时回退默认值，因此 GET
即便没有 settings 行也能返回完整默认结构。
"""

from __future__ import annotations

from app.api.routes import settings as settings_routes
from app.models.project import Project
from app.models.project_membership import ProjectMembership
from app.models.project_settings import ProjectSettings
from app.models.user import User
from app.models.user_password import UserPassword

from tests.support import (
    auth_cookies,
    create_tables,
    make_client,
    make_session_factory,
    make_sqlite_engine,
    make_test_app,
    seed_user,
)


# 所有测试需要建的表：Project/ProjectMembership/ProjectSettings 是核心，
# User/UserPassword 是 seed_user 的依赖。
_MODELS = [
    User,
    UserPassword,
    Project,
    ProjectMembership,
    ProjectSettings,
]


# settings payload 的核心结构键（issubset 断言存在，不锁全部字段集）。
_SETTINGS_KEY_STRUCTURAL = {
    "project_id",
    "world_setting",
    "style_guide",
    "constraints",
    "context_optimizer_enabled",
    "auto_update_worldbook_enabled",
    "auto_update_characters_enabled",
    "auto_update_story_memory_enabled",
    "auto_update_graph_enabled",
    "auto_update_vector_enabled",
    "auto_update_search_enabled",
    "auto_update_fractal_enabled",
    "auto_update_tables_enabled",
    "query_preprocessing",
    "query_preprocessing_default",
    "query_preprocessing_effective",
    "query_preprocessing_effective_source",
}


def _new_client_and_factory():
    """构造一个已建表 + 已登录 u1 的测试 client 与 session factory。"""
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, _MODELS)
    seed_user(factory, user_id="u1")
    app = make_test_app(factory, [settings_routes])
    client = make_client(app)
    auth_cookies(client, "u1")
    return client, factory


def _seed_project(factory, *, project_id: str = "p1", owner_user_id: str = "u1", name: str = "P1") -> str:
    """直接在 DB 插入一个 Project + owner membership，返回 project_id。"""
    with factory() as db:
        db.add(Project(id=project_id, owner_user_id=owner_user_id, name=name))
        db.add(ProjectMembership(project_id=project_id, user_id=owner_user_id, role="owner"))
        db.commit()
    return project_id


def _seed_settings(
    factory,
    *,
    project_id: str = "p1",
    world_setting: str = "WS",
    style_guide: str = "SG",
    constraints: str = "CT",
) -> None:
    """直接在 DB 插入一条 ProjectSettings 行。"""
    with factory() as db:
        db.add(
            ProjectSettings(
                project_id=project_id,
                world_setting=world_setting,
                style_guide=style_guide,
                constraints=constraints,
            )
        )
        db.commit()


# ---------- GET /api/projects/{project_id}/settings ----------


def test_get_settings_returns_ok_with_default_structure_when_no_row() -> None:
    client, _factory = _new_client_and_factory()
    _seed_project(_factory, project_id="p1")
    resp = client.get("/api/projects/p1/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    settings = body["data"]["settings"]
    # 结构键存在（不锁全部字段）
    assert _SETTINGS_KEY_STRUCTURAL.issubset(settings.keys())
    # 行缺失时回退默认值
    assert settings["project_id"] == "p1"
    assert settings["world_setting"] == ""
    assert settings["style_guide"] == ""
    assert settings["constraints"] == ""
    assert settings["context_optimizer_enabled"] is False
    # auto_update_* 默认开启
    assert settings["auto_update_worldbook_enabled"] is True
    assert settings["auto_update_tables_enabled"] is True


def test_get_settings_returns_seeded_text_values() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    _seed_settings(
        factory,
        project_id="p1",
        world_setting="魔法世界",
        style_guide="简洁风格",
        constraints="不要血腥",
    )
    resp = client.get("/api/projects/p1/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    settings = body["data"]["settings"]
    assert _SETTINGS_KEY_STRUCTURAL.issubset(settings.keys())
    assert settings["project_id"] == "p1"
    assert settings["world_setting"] == "魔法世界"
    assert settings["style_guide"] == "简洁风格"
    assert settings["constraints"] == "不要血腥"


# ---------- PUT /api/projects/{project_id}/settings ----------


def test_put_settings_updates_text_fields_and_persists() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    resp = client.put(
        "/api/projects/p1/settings",
        json={
            "world_setting": "新世界观",
            "style_guide": "新风格",
            "constraints": "新约束",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    settings = body["data"]["settings"]
    assert settings["world_setting"] == "新世界观"
    assert settings["style_guide"] == "新风格"
    assert settings["constraints"] == "新约束"

    # 回读验证持久化（再走一次 GET 端点）
    get_resp = client.get("/api/projects/p1/settings")
    assert get_resp.status_code == 200
    get_settings = get_resp.json()["data"]["settings"]
    assert get_settings["world_setting"] == "新世界观"
    assert get_settings["style_guide"] == "新风格"
    assert get_settings["constraints"] == "新约束"


def test_put_settings_toggles_boolean_flags() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    resp = client.put(
        "/api/projects/p1/settings",
        json={
            "context_optimizer_enabled": True,
            "auto_update_worldbook_enabled": False,
            "auto_update_tables_enabled": False,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    settings = body["data"]["settings"]
    assert settings["context_optimizer_enabled"] is True
    assert settings["auto_update_worldbook_enabled"] is False
    assert settings["auto_update_tables_enabled"] is False
    # 未触及的 auto_update_* 保持默认 True
    assert settings["auto_update_characters_enabled"] is True


def test_put_settings_creates_row_when_missing() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    # 项目无 settings 行，PUT 应自动创建
    resp = client.put(
        "/api/projects/p1/settings",
        json={"world_setting": "从零开始"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["settings"]["world_setting"] == "从零开始"
    # DB 层确认行已创建
    with factory() as db:
        row = db.get(ProjectSettings, "p1")
        assert row is not None
        assert row.world_setting == "从零开始"
