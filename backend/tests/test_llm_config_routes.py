"""LLM 配置域路由 happy-path（D 类安全网）。

覆盖 ``app/api/routes`` 下 LLM 配置类路由的主要 CRUD/查询端点：

  llm_profiles.py:
    GET    /api/llm_profiles
    POST   /api/llm_profiles
    PUT    /api/llm_profiles/{profile_id}
    DELETE /api/llm_profiles/{profile_id}

  llm_preset.py:
    GET    /api/projects/{project_id}/llm_preset
    PUT    /api/projects/{project_id}/llm_preset

  llm_models.py:
    GET    /api/llm_models                （仅测缺 key→warning 的安全路径）

  llm_task_presets.py:
    GET    /api/projects/{project_id}/llm_task_presets
    PUT    /api/projects/{project_id}/llm_task_presets/{task_key}
    DELETE /api/projects/{project_id}/llm_task_presets/{task_key}

  llm_capabilities.py:
    GET    /api/llm_capabilities

这些都是当前正确的活功能，测试应保持绿，构成 LLM 配置域的安全网。

跳过项：
- ``GET /api/llm_models`` 携带真实 ``X-LLM-API-Key`` 或已存 api_key 的 profile ——
  会发起真实外部 LLM API 调用（OpenAI/Anthropic/Gemini），超出 happy-path 范围。
  这里只覆盖“未配置 key → 返回 warning + 空模型列表”的纯查询分支。
"""

from __future__ import annotations

from app.api.routes import (
    llm_capabilities as llm_capabilities_routes,
)
from app.api.routes import (
    llm_models as llm_models_routes,
)
from app.api.routes import (
    llm_preset as llm_preset_routes,
)
from app.api.routes import (
    llm_profiles as llm_profiles_routes,
)
from app.api.routes import (
    llm_task_presets as llm_task_presets_routes,
)
from app.models.llm_preset import LLMPreset
from app.models.llm_profile import LLMProfile
from app.models.llm_task_preset import LLMTaskPreset
from app.models.project import Project
from app.models.project_membership import ProjectMembership
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


# 所有测试需要建的表：覆盖 list/create/update/delete 各路径的副作用。
# update_profile / delete_profile 会查询 Project / LLMTaskPreset 表，
# 故即便未绑定也需建表以承载这些查询。
_MODELS = [
    User,
    UserPassword,
    Project,
    ProjectMembership,
    LLMProfile,
    LLMPreset,
    LLMTaskPreset,
]

_ROUTES = [
    llm_profiles_routes,
    llm_preset_routes,
    llm_models_routes,
    llm_task_presets_routes,
    llm_capabilities_routes,
]


def _new_client_and_factory():
    """构造一个已建表 + 已登录 u1 的测试 client 与 session factory。"""
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, _MODELS)
    seed_user(factory, user_id="u1")
    app = make_test_app(factory, _ROUTES)
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


def _seed_profile(factory, *, profile_id: str = "prof1", owner_user_id: str = "u1", name: str = "Prof1") -> str:
    """直接在 DB 插入一个 LLMProfile，返回 profile_id。"""
    with factory() as db:
        db.add(
            LLMProfile(
                id=profile_id,
                owner_user_id=owner_user_id,
                name=name,
                provider="openai",
                base_url="https://api.openai.com/v1",
                model="gpt-4o-mini",
                temperature=0.7,
                top_p=1.0,
                max_tokens=4096,
            )
        )
        db.commit()
    return profile_id


# ---------- GET /api/llm_profiles ----------


def test_list_llm_profiles_empty_returns_empty_list() -> None:
    client, _factory = _new_client_and_factory()
    resp = client.get("/api/llm_profiles")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {"profiles": []}


def test_list_llm_profiles_returns_owned_profile() -> None:
    client, factory = _new_client_and_factory()
    _seed_profile(factory, profile_id="prof1", name="My Profile")
    resp = client.get("/api/llm_profiles")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    profiles = body["data"]["profiles"]
    assert len(profiles) == 1
    p = profiles[0]
    assert p["id"] == "prof1"
    assert p["name"] == "My Profile"
    assert p["owner_user_id"] == "u1"
    assert p["provider"] == "openai"
    assert p["model"] == "gpt-4o-mini"
    assert p["known_model"] is True
    assert p["has_api_key"] is False


# ---------- POST /api/llm_profiles ----------


def test_create_llm_profile_returns_profile_and_persists() -> None:
    client, factory = _new_client_and_factory()
    resp = client.post(
        "/api/llm_profiles",
        json={
            "name": "Created Profile",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "temperature": 0.5,
            "max_tokens": 4096,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    p = body["data"]["profile"]
    assert p["id"]  # new_id 生成的 UUID
    assert p["name"] == "Created Profile"
    assert p["owner_user_id"] == "u1"
    assert p["provider"] == "openai"
    assert p["model"] == "gpt-4o-mini"
    assert p["known_model"] is True
    assert p["temperature"] == 0.5
    assert p["max_tokens"] == 4096
    assert p["has_api_key"] is False
    assert p["masked_api_key"] is None

    # 回读验证持久化
    with factory() as db:
        row = db.get(LLMProfile, p["id"])
        assert row is not None
        assert row.name == "Created Profile"
        assert row.provider == "openai"


def test_create_llm_profile_with_api_key_masks_key() -> None:
    client, _factory = _new_client_and_factory()
    resp = client.post(
        "/api/llm_profiles",
        json={
            "name": "WithKey",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "api_key": "sk-test1234abcd",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    p = body["data"]["profile"]
    assert p["has_api_key"] is True
    masked = p["masked_api_key"]
    assert isinstance(masked, str) and masked
    # mask 格式：前缀****后4位
    assert masked.endswith("abcd")


# ---------- PUT /api/llm_profiles/{profile_id} ----------


def test_update_llm_profile_basic_fields() -> None:
    client, factory = _new_client_and_factory()
    _seed_profile(factory, profile_id="prof1", name="Old")
    resp = client.put(
        "/api/llm_profiles/prof1",
        json={"name": "New Name", "temperature": 0.9},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    p = body["data"]["profile"]
    assert p["id"] == "prof1"
    assert p["name"] == "New Name"
    assert p["temperature"] == 0.9
    # 未改字段保持
    assert p["provider"] == "openai"
    assert p["model"] == "gpt-4o-mini"


# ---------- DELETE /api/llm_profiles/{profile_id} ----------


def test_delete_llm_profile_returns_ok_and_removes_profile() -> None:
    client, factory = _new_client_and_factory()
    _seed_profile(factory, profile_id="prof1", name="ToDelete")
    resp = client.delete("/api/llm_profiles/prof1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {}
    # 回读验证已删除
    with factory() as db:
        assert db.get(LLMProfile, "prof1") is None


# ---------- GET /api/projects/{project_id}/llm_preset ----------


def test_get_llm_preset_returns_transient_default_when_missing() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    resp = client.get("/api/projects/p1/llm_preset")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    preset = body["data"]["llm_preset"]
    assert preset["project_id"] == "p1"
    # 默认 preset 走 openai/gpt-4o-mini 归一化
    assert preset["provider"] == "openai"
    assert preset["model"] == "gpt-4o-mini"
    assert preset["known_model"] is True
    # GET is read-only: the default is a transient response until PUT persists it.
    with factory() as db:
        assert db.get(LLMPreset, "p1") is None


# ---------- PUT /api/projects/{project_id}/llm_preset ----------


def test_put_llm_preset_updates_fields() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    resp = client.put(
        "/api/projects/p1/llm_preset",
        json={
            "provider": "openai",
            "model": "gpt-4o",
            "temperature": 0.3,
            "max_tokens": 2048,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    preset = body["data"]["llm_preset"]
    assert preset["project_id"] == "p1"
    assert preset["model"] == "gpt-4o"
    assert preset["temperature"] == 0.3
    assert preset["max_tokens"] == 2048


# ---------- GET /api/llm_models ----------


def test_list_llm_models_without_key_returns_warning() -> None:
    """profile 未配置 api_key 时，/llm_models 返回 warning + 空模型列表（不触达外部 API）。"""
    client, factory = _new_client_and_factory()
    _seed_profile(factory, profile_id="prof1")  # 无 api_key
    resp = client.get("/api/llm_models", params={"provider": "openai", "profile_id": "prof1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["provider"] == "openai"
    assert data["models"] == []
    assert data["warning"]["code"] == "LLM_KEY_MISSING"


# ---------- GET /api/projects/{project_id}/llm_task_presets ----------


def test_list_llm_task_presets_returns_catalog_and_empty_presets() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    resp = client.get("/api/projects/p1/llm_task_presets")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    # catalog 始终非空（内置任务目录）
    assert isinstance(data["catalog"], list) and len(data["catalog"]) > 0
    catalog_keys = {item["key"] for item in data["catalog"]}
    assert "chapter_generate" in catalog_keys
    # 新项目尚无任务覆盖
    assert data["task_presets"] == []


# ---------- PUT /api/projects/{project_id}/llm_task_presets/{task_key} ----------


def test_put_llm_task_preset_creates_override() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    resp = client.put(
        "/api/projects/p1/llm_task_presets/chapter_generate",
        json={
            "provider": "openai",
            "model": "gpt-4o-mini",
            "temperature": 0.8,
            "max_tokens": 4096,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    tp = body["data"]["task_preset"]
    assert tp["project_id"] == "p1"
    assert tp["task_key"] == "chapter_generate"
    assert tp["provider"] == "openai"
    assert tp["model"] == "gpt-4o-mini"
    assert tp["temperature"] == 0.8
    assert tp["source"] == "task_override"


# ---------- DELETE /api/projects/{project_id}/llm_task_presets/{task_key} ----------


def test_delete_llm_task_preset_removes_override() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    # 先创建一条任务覆盖
    create_resp = client.put(
        "/api/projects/p1/llm_task_presets/chapter_generate",
        json={"provider": "openai", "model": "gpt-4o-mini"},
    )
    assert create_resp.status_code == 200
    # 再删除
    resp = client.delete("/api/projects/p1/llm_task_presets/chapter_generate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {}
    # 回读验证已删除
    with factory() as db:
        assert db.get(LLMTaskPreset, ("p1", "chapter_generate")) is None


# ---------- GET /api/llm_capabilities ----------


def test_get_llm_capabilities_returns_contract_metadata() -> None:
    client, _factory = _new_client_and_factory()
    resp = client.get("/api/llm_capabilities", params={"provider": "openai", "model": "gpt-4o-mini"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    caps = body["data"]["capabilities"]
    expected_keys = {
        "provider",
        "model",
        "provider_key",
        "model_key",
        "known_model",
        "contract_mode",
        "pricing",
        "max_tokens_limit",
        "max_tokens_recommended",
        "context_window_limit",
    }
    assert expected_keys.issubset(caps.keys())
    assert caps["provider"] == "openai"
    assert caps["model"] == "gpt-4o-mini"
    assert caps["known_model"] is True
