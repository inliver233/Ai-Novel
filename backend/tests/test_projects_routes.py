"""projects 域路由 happy-path（D 类安全网）。

覆盖 ``app/api/routes/projects.py`` 的主要 HTTP 端点：

  GET    /api/projects
  GET    /api/projects/summary
  POST   /api/projects
  GET    /api/projects/{project_id}
  GET    /api/projects/{project_id}/memberships
  POST   /api/projects/{project_id}/memberships
  PUT    /api/projects/{project_id}/memberships/{target_user_id}
  DELETE /api/projects/{project_id}/memberships/{target_user_id}
  PUT    /api/projects/{project_id}
  DELETE /api/projects/{project_id}

这些都是当前正确的活功能，测试应保持绿，构成 projects 域的安全网。

跳过项：``POST /api/projects/import_bundle`` —— 依赖 ``import_project_bundle``
服务，需构造完整 bundle 结构、且可能触发向量重建流程，超出 happy-path 范围。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.api.routes import projects as projects_routes
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.knowledge_base import KnowledgeBase
from app.models.llm_preset import LLMPreset
from app.models.llm_profile import LLMProfile
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_membership import ProjectMembership
from app.models.project_settings import ProjectSettings
from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
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


# 所有测试需要建的表：覆盖 create_project 的副作用路径（PromptPreset/PromptBlock/
# KnowledgeBase）以及 summary 的查询路径（Character/Outline/Chapter/LLMPreset/
# LLMProfile/ProjectSettings）。
_MODELS = [
    User,
    UserPassword,
    Project,
    ProjectMembership,
    ProjectSettings,
    Outline,
    Chapter,
    Character,
    LLMProfile,
    LLMPreset,
    PromptPreset,
    PromptBlock,
    KnowledgeBase,
]


def _new_client_and_factory():
    """构造一个已建表 + 已登录 u1 的测试 client 与 session factory。"""
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, _MODELS)
    seed_user(factory, user_id="u1")
    app = make_test_app(factory, [projects_routes])
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


# ---------- GET /api/projects ----------


def test_list_projects_empty_returns_ok_with_empty_list() -> None:
    client, _factory = _new_client_and_factory()
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {"projects": []}


def test_list_projects_returns_owned_project() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1", name="My Novel")
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    projects = body["data"]["projects"]
    assert len(projects) == 1
    assert projects[0]["id"] == "p1"
    assert projects[0]["name"] == "My Novel"
    assert projects[0]["owner_user_id"] == "u1"


# ---------- GET /api/projects/summary ----------


def test_list_projects_summary_empty_returns_empty_items() -> None:
    client, _factory = _new_client_and_factory()
    resp = client.get("/api/projects/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {"items": []}


def test_list_projects_summary_returns_item_with_expected_shape() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1", name="Sum Novel")
    resp = client.get("/api/projects/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    items = body["data"]["items"]
    assert len(items) == 1
    item = items[0]
    # 断言结构关键键（不锁文案）
    expected_keys = {
        "project",
        "settings",
        "characters_count",
        "outline_content_md",
        "outline_content_len",
        "outline_content_truncated",
        "chapters_total",
        "chapters_done",
        "llm_preset",
        "llm_profile_has_api_key",
    }
    assert expected_keys.issubset(item.keys())
    assert item["project"]["id"] == "p1"
    assert item["project"]["name"] == "Sum Novel"
    assert item["characters_count"] == 0
    assert item["chapters_total"] == 0
    assert item["chapters_done"] == 0
    assert item["outline_content_md"] == ""
    assert item["outline_content_truncated"] is False


# ---------- POST /api/projects ----------


def test_create_project_returns_project_and_persists() -> None:
    client, factory = _new_client_and_factory()
    resp = client.post(
        "/api/projects",
        json={"name": "Created Novel", "genre": "scifi", "logline": "A logline."},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    project = body["data"]["project"]
    assert project["id"]  # new_id 生成的 UUID
    assert project["name"] == "Created Novel"
    assert project["genre"] == "scifi"
    assert project["logline"] == "A logline."
    assert project["owner_user_id"] == "u1"

    # 回读验证持久化
    with factory() as db:
        row = db.get(Project, project["id"])
        assert row is not None
        assert row.name == "Created Novel"
        assert row.genre == "scifi"
        memberships = (
            db.execute(select(ProjectMembership).where(ProjectMembership.project_id == project["id"])).scalars().all()
        )
        assert [(row.user_id, row.role) for row in memberships] == [("u1", "owner")]
        presets = db.execute(select(PromptPreset).where(PromptPreset.project_id == project["id"])).scalars().all()
        assert len({row.resource_key for row in presets}) == 6
        assert all(db.execute(select(PromptBlock.id).where(PromptBlock.preset_id == row.id)).first() for row in presets)
        active_tasks = {task for row in presets for task in json.loads(row.active_for_json or "[]")}
        assert active_tasks == {
            "plan_chapter",
            "post_edit",
            "content_optimize",
            "outline_generate",
            "chapter_generate",
            "detailed_outline_generate",
        }
        assert db.execute(
            select(KnowledgeBase).where(KnowledgeBase.project_id == project["id"], KnowledgeBase.kb_id == "default")
        ).scalar_one()


@pytest.mark.parametrize("failure", ["prompt", "kb"])
def test_create_project_initialization_failure_rolls_back_everything(monkeypatch, failure: str) -> None:
    client, factory = _new_client_and_factory()
    target = "stage_missing_builtin_prompt_defaults" if failure == "prompt" else "ensure_default_kb"
    monkeypatch.setattr(projects_routes, target, lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(failure)))
    with pytest.raises(RuntimeError, match=failure):
        client.post("/api/projects", json={"name": "Atomic"})
    with factory() as observer:
        assert observer.execute(select(Project).where(Project.name == "Atomic")).first() is None
        assert observer.execute(select(ProjectMembership)).first() is None
        assert observer.execute(select(PromptPreset)).first() is None
        assert observer.execute(select(PromptBlock)).first() is None
        assert observer.execute(select(KnowledgeBase)).first() is None


# ---------- GET /api/projects/{project_id} ----------


def test_get_project_returns_project() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1", name="Gettable")
    resp = client.get("/api/projects/p1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["project"]["id"] == "p1"
    assert body["data"]["project"]["name"] == "Gettable"
    assert body["data"]["project"]["owner_user_id"] == "u1"


# ---------- GET /api/projects/{project_id}/memberships ----------


def test_list_project_memberships_includes_owner() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    resp = client.get("/api/projects/p1/memberships")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    memberships = body["data"]["memberships"]
    assert len(memberships) == 1
    m = memberships[0]
    assert m["project_id"] == "p1"
    assert m["user"]["id"] == "u1"
    assert m["role"] == "owner"


# ---------- POST /api/projects/{project_id}/memberships ----------


def test_add_project_membership_for_existing_user() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    seed_user(factory, user_id="u2")
    resp = client.post(
        "/api/projects/p1/memberships",
        json={"user_id": "u2", "role": "viewer"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    m = body["data"]["membership"]
    assert m["project_id"] == "p1"
    assert m["user"]["id"] == "u2"
    assert m["role"] == "viewer"


# ---------- PUT /api/projects/{project_id}/memberships/{target_user_id} ----------


def test_update_project_membership_role() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    seed_user(factory, user_id="u2")
    # 先添加为 viewer
    add_resp = client.post(
        "/api/projects/p1/memberships",
        json={"user_id": "u2", "role": "viewer"},
    )
    assert add_resp.status_code == 200
    # 升级为 editor
    resp = client.put(
        "/api/projects/p1/memberships/u2",
        json={"role": "editor"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["membership"]["role"] == "editor"
    assert body["data"]["membership"]["user"]["id"] == "u2"


# ---------- DELETE /api/projects/{project_id}/memberships/{target_user_id} ----------


def test_remove_project_membership() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    seed_user(factory, user_id="u2")
    client.post(
        "/api/projects/p1/memberships",
        json={"user_id": "u2", "role": "viewer"},
    )
    resp = client.delete("/api/projects/p1/memberships/u2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {}
    # 回读验证已删除
    with factory() as db:
        assert db.get(ProjectMembership, ("p1", "u2")) is None


# ---------- PUT /api/projects/{project_id} ----------


def test_update_project_basic_fields() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1", name="Old")
    resp = client.put(
        "/api/projects/p1",
        json={"name": "New Name", "genre": "fantasy", "logline": "New logline."},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    project = body["data"]["project"]
    assert project["id"] == "p1"
    assert project["name"] == "New Name"
    assert project["genre"] == "fantasy"
    assert project["logline"] == "New logline."


# ---------- DELETE /api/projects/{project_id} ----------


def test_delete_project_returns_ok_and_removes_project() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1", name="ToDelete")
    resp = client.delete("/api/projects/p1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {}
    # 回读验证已删除
    with factory() as db:
        assert db.get(Project, "p1") is None
