"""prompts/prompt_studio 域路由 happy-path（D 类安全网）。

覆盖两个路由模块的主要 CRUD/查询类 HTTP 端点：

``app/api/routes/prompts.py``:

  GET    /api/projects/{project_id}/prompt_presets
  GET    /api/projects/{project_id}/prompt_preset_resources
  POST   /api/projects/{project_id}/prompt_presets
  GET    /api/prompt_presets/{preset_id}
  PUT    /api/prompt_presets/{preset_id}
  DELETE /api/prompt_presets/{preset_id}
  POST   /api/prompt_presets/{preset_id}/blocks
  PUT    /api/prompt_blocks/{block_id}
  DELETE /api/prompt_blocks/{block_id}
  POST   /api/prompt_presets/{preset_id}/blocks/reorder
  GET    /api/prompt_presets/{preset_id}/export
  POST   /api/projects/{project_id}/prompt_presets/import
  GET    /api/projects/{project_id}/prompt_presets/export_all
  POST   /api/projects/{project_id}/prompt_presets/import_all
  POST   /api/prompt_presets/{preset_id}/reset_to_default
  POST   /api/prompt_blocks/{block_id}/reset_to_default
  POST   /api/projects/{project_id}/prompt_preview

``app/api/routes/prompt_studio.py``:

  GET    /api/projects/{project_id}/prompt-studio/categories
  POST   /api/projects/{project_id}/prompt-studio/presets
  GET    /api/projects/{project_id}/prompt-studio/presets/{preset_id}
  PUT    /api/projects/{project_id}/prompt-studio/presets/{preset_id}
  PUT    /api/projects/{project_id}/prompt-studio/presets/{preset_id}/activate
  DELETE /api/projects/{project_id}/prompt-studio/presets/{preset_id}

这些都是当前正确的活功能，测试应保持绿，构成 prompts 域安全网。

设计要点：
  - 只断言结构化字段（id/name/scope/version 等），不锁中文文案。
  - 创建/更新后回读验证持久化。
  - prompt_studio 用 ``outline_generate`` 分类（无 output_contract 切分，不触发
    M28/preset 契约 known bug —— 见 tests/test_prompt_studio_routes.py）。
  - prompt_preview 经核查（``app/services/prompt_preset_render.py``）只做本地
    模板渲染 + provider 元数据查询，不调用外部 LLM，故可纳入 happy-path。
"""

from __future__ import annotations

from app.api.routes import prompt_studio as prompt_studio_routes
from app.api.routes import prompts as prompts_routes
from app.models.llm_preset import LLMPreset
from app.models.project import Project
from app.models.project_default_style import ProjectDefaultStyle
from app.models.project_membership import ProjectMembership
from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
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


# 所有测试需要建的表：prompts 路由副作用（PromptPreset/PromptBlock）、prompt_studio
# 路由副作用（WritingStyle/ProjectDefaultStyle）、以及 preview 路径读取的 LLMPreset
# （db.get(LLMPreset, project_id)，无此表会 OperationalError）。
_MODELS = [
    User,
    UserPassword,
    Project,
    ProjectMembership,
    PromptPreset,
    PromptBlock,
    WritingStyle,
    ProjectDefaultStyle,
    LLMPreset,
]


def _new_client_and_factory():
    """构造一个已建表 + 已登录 u1 的测试 client 与 session factory。

    同时挂载 prompts 与 prompt_studio 两个路由模块，前置 /api 前缀。
    """
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, _MODELS)
    seed_user(factory, user_id="u1")
    app = make_test_app(factory, [prompts_routes, prompt_studio_routes])
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


# ===========================================================================
# prompts.py — preset 列表/资源/详情
# ===========================================================================


def test_list_prompt_presets_returns_ok_with_preset_list() -> None:
    """Explicit sync initializes defaults; the subsequent GET returns them."""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    sync_resp = client.post("/api/projects/p1/prompt_presets/sync_builtin_defaults")
    assert sync_resp.status_code == 200
    resp = client.get("/api/projects/p1/prompt_presets")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    presets = body["data"]["presets"]
    assert isinstance(presets, list)
    assert len(presets) >= 1
    # 断言结构关键键
    expected_keys = {"id", "project_id", "name", "scope", "version"}
    assert expected_keys.issubset(presets[0].keys())
    assert presets[0]["project_id"] == "p1"


def test_list_prompt_preset_resources_returns_ok_with_resources() -> None:
    """GET /api/projects/{project_id}/prompt_preset_resources 返回可用资源清单。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    resp = client.get("/api/projects/p1/prompt_preset_resources")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    resources = body["data"]["resources"]
    assert isinstance(resources, list)
    assert len(resources) >= 1
    expected_keys = {"key", "name", "scope", "version", "activation_tasks"}
    assert expected_keys.issubset(resources[0].keys())


# ===========================================================================
# prompts.py — preset CRUD
# ===========================================================================


def test_create_prompt_preset_returns_preset_and_persists() -> None:
    """POST /api/projects/{project_id}/prompt_presets 创建并返回 preset。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    resp = client.post(
        "/api/projects/p1/prompt_presets",
        json={"name": "My Preset", "scope": "project", "version": 1, "active_for": ["outline_generate"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    preset = body["data"]["preset"]
    assert preset["id"]
    assert preset["name"] == "My Preset"
    assert preset["project_id"] == "p1"
    assert preset["scope"] == "project"
    assert preset["active_for"] == ["outline_generate"]
    # 回读验证持久化
    with factory() as db:
        row = db.get(PromptPreset, preset["id"])
        assert row is not None
        assert row.name == "My Preset"


def test_get_prompt_preset_returns_preset_with_blocks() -> None:
    """GET /api/prompt_presets/{preset_id} 返回 preset 详情与块列表。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    create_resp = client.post(
        "/api/projects/p1/prompt_presets",
        json={"name": "Detail Preset", "scope": "project"},
    )
    preset_id = create_resp.json()["data"]["preset"]["id"]
    resp = client.get(f"/api/prompt_presets/{preset_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["preset"]["id"] == preset_id
    assert data["preset"]["name"] == "Detail Preset"
    assert "blocks" in data
    assert isinstance(data["blocks"], list)


def test_update_prompt_preset_basic_fields() -> None:
    """PUT /api/prompt_presets/{preset_id} 更新 preset 基本字段。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    create_resp = client.post(
        "/api/projects/p1/prompt_presets",
        json={"name": "Old", "scope": "project"},
    )
    preset_id = create_resp.json()["data"]["preset"]["id"]
    resp = client.put(
        f"/api/prompt_presets/{preset_id}",
        json={"name": "New Name", "scope": "project", "version": 2, "active_for": ["chapter_generate"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    preset = body["data"]["preset"]
    assert preset["name"] == "New Name"
    assert preset["version"] == 2
    assert preset["active_for"] == ["chapter_generate"]


def test_delete_prompt_preset_returns_ok_and_removes_preset() -> None:
    """DELETE /api/prompt_presets/{preset_id} 删除 preset。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    create_resp = client.post(
        "/api/projects/p1/prompt_presets",
        json={"name": "To Delete", "scope": "project"},
    )
    preset_id = create_resp.json()["data"]["preset"]["id"]
    resp = client.delete(f"/api/prompt_presets/{preset_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {}
    # 回读验证已删除
    with factory() as db:
        assert db.get(PromptPreset, preset_id) is None


# ===========================================================================
# prompts.py — block CRUD
# ===========================================================================


def test_create_prompt_block_returns_block_and_persists() -> None:
    """POST /api/prompt_presets/{preset_id}/blocks 创建块。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    preset_id = client.post(
        "/api/projects/p1/prompt_presets", json={"name": "P", "scope": "project"}
    ).json()["data"]["preset"]["id"]
    resp = client.post(
        f"/api/prompt_presets/{preset_id}/blocks",
        json={
            "identifier": "sys.test.role",
            "name": "Test Role",
            "role": "system",
            "enabled": True,
            "template": "You are a test assistant.",
            "injection_position": "relative",
            "injection_order": 0,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    block = body["data"]["block"]
    assert block["id"]
    assert block["preset_id"] == preset_id
    assert block["identifier"] == "sys.test.role"
    assert block["name"] == "Test Role"
    assert block["role"] == "system"
    assert block["enabled"] is True
    # 回读验证持久化
    with factory() as db:
        assert db.get(PromptBlock, block["id"]) is not None


def test_update_prompt_block_basic_fields() -> None:
    """PUT /api/prompt_blocks/{block_id} 更新块字段。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    preset_id = client.post(
        "/api/projects/p1/prompt_presets", json={"name": "P", "scope": "project"}
    ).json()["data"]["preset"]["id"]
    block_id = client.post(
        f"/api/prompt_presets/{preset_id}/blocks",
        json={"identifier": "sys.test", "name": "Old", "role": "system"},
    ).json()["data"]["block"]["id"]
    resp = client.put(
        f"/api/prompt_blocks/{block_id}",
        json={"name": "New Name", "enabled": False, "template": "Updated template."},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    block = body["data"]["block"]
    assert block["name"] == "New Name"
    assert block["enabled"] is False
    assert block["template"] == "Updated template."


def test_delete_prompt_block_returns_ok_and_removes_block() -> None:
    """DELETE /api/prompt_blocks/{block_id} 删除块。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    preset_id = client.post(
        "/api/projects/p1/prompt_presets", json={"name": "P", "scope": "project"}
    ).json()["data"]["preset"]["id"]
    block_id = client.post(
        f"/api/prompt_presets/{preset_id}/blocks",
        json={"identifier": "sys.test", "name": "B", "role": "system"},
    ).json()["data"]["block"]["id"]
    resp = client.delete(f"/api/prompt_blocks/{block_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {}
    with factory() as db:
        assert db.get(PromptBlock, block_id) is None


def test_reorder_prompt_blocks_returns_reordered_list() -> None:
    """POST /api/prompt_presets/{preset_id}/blocks/reorder 重排序块。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    preset_id = client.post(
        "/api/projects/p1/prompt_presets", json={"name": "P", "scope": "project"}
    ).json()["data"]["preset"]["id"]
    b1 = client.post(
        f"/api/prompt_presets/{preset_id}/blocks",
        json={"identifier": "sys.b1", "name": "B1", "role": "system", "injection_order": 0},
    ).json()["data"]["block"]["id"]
    b2 = client.post(
        f"/api/prompt_presets/{preset_id}/blocks",
        json={"identifier": "sys.b2", "name": "B2", "role": "system", "injection_order": 1},
    ).json()["data"]["block"]["id"]
    # 反转顺序
    resp = client.post(
        f"/api/prompt_presets/{preset_id}/blocks/reorder",
        json={"ordered_block_ids": [b2, b1]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    blocks = body["data"]["blocks"]
    assert [b["id"] for b in blocks] == [b2, b1]
    assert blocks[0]["injection_order"] == 0
    assert blocks[1]["injection_order"] == 1


# ===========================================================================
# prompts.py — export / import
# ===========================================================================


def test_export_prompt_preset_returns_ok_with_export_model() -> None:
    """GET /api/prompt_presets/{preset_id}/export 导出单个 preset。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    preset_id = client.post(
        "/api/projects/p1/prompt_presets", json={"name": "Export Me", "scope": "project"}
    ).json()["data"]["preset"]["id"]
    resp = client.get(f"/api/prompt_presets/{preset_id}/export")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    export = body["data"]["export"]
    assert export["preset"]["name"] == "Export Me"
    assert "blocks" in export
    assert isinstance(export["blocks"], list)


def test_import_prompt_preset_returns_ok_and_creates_preset() -> None:
    """POST /api/projects/{project_id}/prompt_presets/import 导入单个 preset。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    resp = client.post(
        "/api/projects/p1/prompt_presets/import",
        json={
            "preset": {"name": "Imported", "scope": "project", "version": 1, "active_for": []},
            "blocks": [
                {
                    "identifier": "sys.imported",
                    "name": "Imported Block",
                    "role": "system",
                    "enabled": True,
                    "template": "Hello.",
                    "injection_position": "relative",
                    "injection_order": 0,
                }
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    preset = body["data"]["preset"]
    assert preset["id"]
    assert preset["name"] == "Imported"
    assert preset["project_id"] == "p1"
    # 回读验证持久化
    with factory() as db:
        assert db.get(PromptPreset, preset["id"]) is not None


def test_export_all_prompt_presets_returns_ok_with_schema_version() -> None:
    """GET /api/projects/{project_id}/prompt_presets/export_all 导出全部。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    client.post("/api/projects/p1/prompt_presets", json={"name": "P1", "scope": "project"})
    client.post("/api/projects/p1/prompt_presets", json={"name": "P2", "scope": "project"})
    resp = client.get("/api/projects/p1/prompt_presets/export_all")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    export = body["data"]["export"]
    assert export["schema_version"] == "prompt_presets_export_all_v1"
    assert isinstance(export["presets"], list)
    assert len(export["presets"]) >= 2


def test_import_all_prompt_presets_creates_new_presets() -> None:
    """POST /api/projects/{project_id}/prompt_presets/import_all 批量导入（创建分支）。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    resp = client.post(
        "/api/projects/p1/prompt_presets/import_all",
        json={
            "schema_version": "prompt_presets_export_all_v1",
            "dry_run": False,
            "presets": [
                {
                    "preset": {"name": "Bulk A", "scope": "project", "version": 1, "active_for": []},
                    "blocks": [],
                },
                {
                    "preset": {"name": "Bulk B", "scope": "project", "version": 1, "active_for": []},
                    "blocks": [],
                },
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["dry_run"] is False
    assert data["created"] == 2
    assert data["updated"] == 0
    assert data["skipped"] == 0


def test_import_all_prompt_presets_dry_run_does_not_persist() -> None:
    """POST /api/projects/{project_id}/prompt_presets/import_all dry_run 不落库。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    resp = client.post(
        "/api/projects/p1/prompt_presets/import_all",
        json={
            "schema_version": "prompt_presets_export_all_v1",
            "dry_run": True,
            "presets": [
                {
                    "preset": {"name": "Dry Run", "scope": "project", "version": 1, "active_for": []},
                    "blocks": [],
                }
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["dry_run"] is True
    assert data["created"] == 1
    # dry_run 不应真正写入
    with factory() as db:
        rows = db.query(PromptPreset).filter(PromptPreset.name == "Dry Run").all()
        assert rows == []


# ===========================================================================
# prompts.py — reset_to_default（依赖内置资源，用 baseline 产生的 preset）
# ===========================================================================


def test_reset_prompt_preset_to_default_returns_ok_with_preset_and_blocks() -> None:
    """POST /api/prompt_presets/{preset_id}/reset_to_default 重置 preset 到默认资源。

    依赖 preset 有 resource_key；list 端点会触发 baseline ensurer 创建带 resource_key
    的默认 preset，取其一来重置。
    """
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    sync_resp = client.post("/api/projects/p1/prompt_presets/sync_builtin_defaults")
    assert sync_resp.status_code == 200
    list_resp = client.get("/api/projects/p1/prompt_presets")
    presets = list_resp.json()["data"]["presets"]
    # 找一个有 resource_key 的 preset
    target = next((p for p in presets if p.get("resource_key")), None)
    assert target is not None, "baseline 应至少产生一个带 resource_key 的 preset"
    resp = client.post(f"/api/prompt_presets/{target['id']}/reset_to_default")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["preset"]["id"] == target["id"]
    assert isinstance(data["blocks"], list)


def test_reset_prompt_block_to_default_returns_ok_with_block() -> None:
    """POST /api/prompt_blocks/{block_id}/reset_to_default 重置块到默认资源。

    依赖块所属 preset 有 resource_key；baseline preset 的块都满足此条件。
    """
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    sync_resp = client.post("/api/projects/p1/prompt_presets/sync_builtin_defaults")
    assert sync_resp.status_code == 200
    list_resp = client.get("/api/projects/p1/prompt_presets")
    presets = list_resp.json()["data"]["presets"]
    target = next((p for p in presets if p.get("resource_key")), None)
    assert target is not None
    # 取该 preset 的第一个块
    detail = client.get(f"/api/prompt_presets/{target['id']}").json()["data"]
    block_id = detail["blocks"][0]["id"]
    resp = client.post(f"/api/prompt_blocks/{block_id}/reset_to_default")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    block = body["data"]["block"]
    assert block["id"] == block_id


# ===========================================================================
# prompts.py — prompt_preview（本地渲染，不调外部 LLM）
# ===========================================================================


def test_preview_prompt_returns_ok_with_preview_payload() -> None:
    """POST /api/projects/{project_id}/prompt_preview 本地渲染预览。

    经核查 app/services/prompt_preset_render.py：render_preset_for_task 只做本地
    模板渲染 + provider 元数据查询（max_context_tokens_limit 等），不调用外部 LLM。
    使用 content_optimize 任务——其 baseline preset 默认 activate=True
    （见 prompt_preset_defaults.ensure_default_content_optimize_preset），满足
    render_preset_for_task 的 "preset 已配置" 前置；outline/chapter 默认不激活故避开。
    """
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    sync_resp = client.post("/api/projects/p1/prompt_presets/sync_builtin_defaults")
    assert sync_resp.status_code == 200
    resp = client.post(
        "/api/projects/p1/prompt_preview",
        json={"task": "content_optimize", "values": {}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert "preview" in data
    assert "render_log" in data
    preview = data["preview"]
    assert preview["task"] == "content_optimize"
    assert isinstance(preview["system"], str)
    assert isinstance(preview["user"], str)


# ===========================================================================
# prompt_studio.py — categories
# ===========================================================================


def test_prompt_studio_list_categories_returns_ok_with_category_keys() -> None:
    """GET /api/projects/{project_id}/prompt-studio/categories 返回分类清单。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    resp = client.get("/api/projects/p1/prompt-studio/categories")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    categories = body["data"]["categories"]
    keys = {c["key"] for c in categories}
    # 断言关键分类键存在（不锁文案 label）
    assert {"outline_generate", "chapter_generate", "plan_chapter", "post_edit", "writing_style"}.issubset(keys)
    for c in categories:
        assert "presets" in c
        assert isinstance(c["presets"], list)


# ===========================================================================
# prompt_studio.py — preset CRUD（用 outline_generate 分类，规避 M28/preset 契约 bug）
# ===========================================================================


def test_prompt_studio_create_preset_returns_preset_detail() -> None:
    """POST /api/projects/{project_id}/prompt-studio/presets 创建 Studio preset。

    用 outline_generate 分类：该分类没有独立的结构化输出合同区，整段 guidance
    可编辑；带合同的 plan/post-edit/content-optimize 路径由专门 round-trip 测试覆盖。
    """
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    resp = client.post(
        "/api/projects/p1/prompt-studio/presets?category=outline_generate",
        json={"name": "My Outline Style", "content": "Write a compelling outline."},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    preset = body["data"]["preset"]
    assert preset["id"]
    assert preset["name"] == "My Outline Style"
    assert preset["content"] == "Write a compelling outline."
    assert preset["is_active"] is False


def test_prompt_studio_get_preset_returns_preset_detail() -> None:
    """GET /api/projects/{project_id}/prompt-studio/presets/{preset_id} 返回详情。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    preset_id = client.post(
        "/api/projects/p1/prompt-studio/presets?category=outline_generate",
        json={"name": "G", "content": "Content A."},
    ).json()["data"]["preset"]["id"]
    resp = client.get(
        f"/api/projects/p1/prompt-studio/presets/{preset_id}?category=outline_generate"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    preset = body["data"]["preset"]
    assert preset["id"] == preset_id
    assert preset["content"] == "Content A."


def test_prompt_studio_update_preset_name_and_content() -> None:
    """PUT /api/projects/{project_id}/prompt-studio/presets/{preset_id} 更新。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    preset_id = client.post(
        "/api/projects/p1/prompt-studio/presets?category=outline_generate",
        json={"name": "Old", "content": "Old content."},
    ).json()["data"]["preset"]["id"]
    resp = client.put(
        f"/api/projects/p1/prompt-studio/presets/{preset_id}",
        json={"name": "New", "content": "New content."},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    preset = body["data"]["preset"]
    assert preset["name"] == "New"
    assert preset["content"] == "New content."


def test_prompt_studio_activate_preset_sets_is_active_true() -> None:
    """PUT /api/projects/{project_id}/prompt-studio/presets/{preset_id}/activate 激活。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    preset_id = client.post(
        "/api/projects/p1/prompt-studio/presets?category=outline_generate",
        json={"name": "Act", "content": "Activate me."},
    ).json()["data"]["preset"]["id"]
    resp = client.put(
        f"/api/projects/p1/prompt-studio/presets/{preset_id}/activate?category=outline_generate"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    preset = body["data"]["preset"]
    assert preset["is_active"] is True


def test_prompt_studio_delete_preset_returns_ok_and_removes_preset() -> None:
    """DELETE /api/projects/{project_id}/prompt-studio/presets/{preset_id} 删除。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    preset_id = client.post(
        "/api/projects/p1/prompt-studio/presets?category=outline_generate",
        json={"name": "Del", "content": "Delete me."},
    ).json()["data"]["preset"]["id"]
    resp = client.delete(f"/api/projects/p1/prompt-studio/presets/{preset_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {}
    with factory() as db:
        assert db.get(PromptPreset, preset_id) is None


# ===========================================================================
# prompt_studio.py — writing_style 分类（preset-free 路径，与既有测试互补）
# ===========================================================================


def test_prompt_studio_writing_style_crud_returns_ok() -> None:
    """writing_style 分类的 create/get/update/delete happy-path。

    与 tests/test_prompt_studio_routes.py 的 writing_style 测试互补：本测试用
    tests.support 脚手架（pytest 函数式），断言结构化字段。writing_style 分类
    走 WritingStyle 表路径，不依赖 prompt preset 资源，无 known_issue。
    """
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    # create
    create_resp = client.post(
        "/api/projects/p1/prompt-studio/presets?category=writing_style",
        json={"name": "My Style", "content": "Concise and vivid prose."},
    )
    assert create_resp.status_code == 200
    style_id = create_resp.json()["data"]["preset"]["id"]
    assert create_resp.json()["data"]["preset"]["content"] == "Concise and vivid prose."
    # get
    get_resp = client.get(
        f"/api/projects/p1/prompt-studio/presets/{style_id}?category=writing_style"
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["data"]["preset"]["id"] == style_id
    # activate
    act_resp = client.put(
        f"/api/projects/p1/prompt-studio/presets/{style_id}/activate?category=writing_style"
    )
    assert act_resp.status_code == 200
    assert act_resp.json()["data"]["preset"]["is_active"] is True
    # update
    upd_resp = client.put(
        f"/api/projects/p1/prompt-studio/presets/{style_id}",
        json={"name": "My Style v2", "content": "More lyrical."},
    )
    assert upd_resp.status_code == 200
    updated = upd_resp.json()["data"]["preset"]
    assert updated["name"] == "My Style v2"
    assert updated["content"] == "More lyrical."
    # delete
    del_resp = client.delete(f"/api/projects/p1/prompt-studio/presets/{style_id}")
    assert del_resp.status_code == 200
    assert del_resp.json()["data"] == {}
    with factory() as db:
        assert db.get(WritingStyle, style_id) is None
