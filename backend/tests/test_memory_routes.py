"""memory 域路由 happy-path（D 类安全网）。

覆盖两个路由模块的主要 HTTP 端点：

``app/api/routes/memory.py``:
  GET  /api/projects/{project_id}/memory/retrieve
  POST /api/projects/{project_id}/memory/preview
  POST /api/projects/{project_id}/story_memories/import_all

``app/api/routes/story_memory.py``:
  GET   /api/projects/{project_id}/story_memories
  POST  /api/projects/{project_id}/story_memories
  PUT   /api/projects/{project_id}/story_memories/{story_memory_id}
  DELETE /api/projects/{project_id}/story_memories/{story_memory_id}
  POST  /api/projects/{project_id}/story_memories/merge
  POST  /api/projects/{project_id}/story_memories/{story_memory_id}/mark_done

这些都是当前正确的活功能，测试应保持绿，构成 memory 域的安全网。

关于向量/pgvector/外部 LLM 依赖：
  - retrieve/preview 内部会调用 ``retrieve_memory_context_pack``，其中 story_memory
    段是纯 DB 查询（按 importance 排序，可被 query_text 过滤），vector_rag /
    semantic_history 段在向量后端不可用时优雅降级（try/except → disabled）。
    故这两个端点在无向量环境仍是真实可测的 happy-path，此处断言 story_memory 段
    的真实数据与顶层 pack 结构，不断言 vector_rag 内部细节。
  - story_memories 的 CRUD/merge/mark_done/import_all 在写库后调用
    ``schedule_vector_rebuild_task`` / ``schedule_search_rebuild_task``，二者均为
    fail-soft 调度器（捕获所有异常、返回 None）。为让这些副作用真实落库（ProjectTask /
    ProjectTaskEvent），本文件显式建立相关活表，符合“有 fail-soft 副作用写多表则全建活表”的约定。
"""

from __future__ import annotations

from app.api.routes import memory as memory_routes
from app.api.routes import story_memory as story_memory_routes
from app.models.project import Project
from app.models.project_membership import ProjectMembership
from app.models.project_settings import ProjectSettings
from app.models.project_task import ProjectTask
from app.models.project_task_event import ProjectTaskEvent
from app.models.story_memory import StoryMemory
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


# 覆盖 story_memories CRUD 的副作用路径：
# - ProjectSettings：``_mark_vector_index_dirty`` 标记 vector_index_dirty
# - ProjectTask / ProjectTaskEvent：fail-soft 重建调度器落库
# memory retrieve/preview 仅需 StoryMemory + ProjectSettings 做纯查询，同样已包含。
_MODELS = [
    User,
    UserPassword,
    Project,
    ProjectMembership,
    ProjectSettings,
    StoryMemory,
    ProjectTask,
    ProjectTaskEvent,
]


def _new_client_and_factory():
    """构造一个已建表 + 已登录 u1 的测试 client 与 session factory。"""
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, _MODELS)
    seed_user(factory, user_id="u1")
    app = make_test_app(factory, [memory_routes, story_memory_routes])
    client = make_client(app)
    auth_cookies(client, "u1")
    return client, factory


def _seed_project(factory, *, project_id: str = "p1", name: str = "P1") -> str:
    """直接在 DB 插入一个 Project + owner membership，返回 project_id。"""
    with factory() as db:
        db.add(Project(id=project_id, owner_user_id="u1", name=name))
        db.add(ProjectMembership(project_id=project_id, user_id="u1", role="owner"))
        db.commit()
    return project_id


def _seed_story_memory(
    factory,
    *,
    project_id: str = "p1",
    memory_id: str = "m1",
    memory_type: str = "event",
    title: str | None = "Memory",
    content: str = "A memory entry.",
    importance_score: float = 1.0,
    chapter_id: str | None = None,
    tags_json: str | None = None,
) -> str:
    """直接在 DB 插入一条 StoryMemory，返回 memory_id。"""
    with factory() as db:
        db.add(
            StoryMemory(
                id=memory_id,
                project_id=project_id,
                chapter_id=chapter_id,
                memory_type=memory_type,
                title=title,
                content=content,
                importance_score=importance_score,
                tags_json=tags_json,
            )
        )
        db.commit()
    return memory_id


# 顶层 pack 应包含的三段 + 日志（结构键，不锁文案）
_PACK_TOP_KEYS = {"story_memory", "semantic_history", "vector_rag", "logs"}


# ====================================================================
# memory.py —— GET /api/projects/{project_id}/memory/retrieve
# ====================================================================


def test_retrieve_memory_returns_pack_with_story_memory_section() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    _seed_story_memory(factory, memory_id="m1", content="主角发现魔法森林的入口", importance_score=5.0)

    resp = client.get("/api/projects/p1/memory/retrieve", params={"query_text": "魔法森林"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert _PACK_TOP_KEYS.issubset(data.keys())

    story_memory = data["story_memory"]
    assert story_memory["enabled"] is True
    assert len(story_memory["items"]) >= 1
    assert story_memory["items"][0]["id"] == "m1"

    # logs 覆盖三段
    logged_sections = {entry["section"] for entry in data["logs"]}
    assert {"story_memory", "semantic_history", "vector_rag"}.issubset(logged_sections)


def test_retrieve_memory_on_empty_project_returns_disabled_story_memory() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)

    resp = client.get("/api/projects/p1/memory/retrieve", params={"query_text": "anything"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    story_memory = body["data"]["story_memory"]
    assert story_memory["enabled"] is False
    assert story_memory["items"] == []


# ====================================================================
# memory.py —— POST /api/projects/{project_id}/memory/preview
# ====================================================================


def test_preview_memory_returns_pack_with_section_control() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    _seed_story_memory(factory, memory_id="m1", content="关键设定：世界树", importance_score=3.0)

    resp = client.post(
        "/api/projects/p1/memory/preview",
        json={
            "query_text": "世界树",
            "section_enabled": {"story_memory": True, "vector_rag": False},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    story_memory = body["data"]["story_memory"]
    assert story_memory["enabled"] is True
    assert any(item["id"] == "m1" for item in story_memory["items"])

    # 显式关闭 vector_rag → disabled 段
    vector_rag = body["data"]["vector_rag"]
    assert vector_rag["enabled"] is False


# ====================================================================
# story_memory.py —— GET /api/projects/{project_id}/story_memories
# ====================================================================


def test_list_story_memories_empty() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)

    resp = client.get("/api/projects/p1/story_memories")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {"items": [], "next_offset": None}


def test_list_story_memories_returns_seeded_rows() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    _seed_story_memory(factory, memory_id="m1", title="First")
    _seed_story_memory(factory, memory_id="m2", title="Second")

    resp = client.get("/api/projects/p1/story_memories")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    items = body["data"]["items"]
    assert len(items) == 2
    ids = {item["id"] for item in items}
    assert ids == {"m1", "m2"}
    # 结构键存在（不锁文案）
    expected_keys = {
        "id",
        "project_id",
        "memory_type",
        "title",
        "content",
        "importance_score",
        "tags",
        "is_foreshadow",
        "done",
    }
    assert expected_keys.issubset(items[0].keys())


def test_list_story_memories_filters_by_chapter_id() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    _seed_story_memory(factory, memory_id="m1", chapter_id="c1")
    _seed_story_memory(factory, memory_id="m2", chapter_id=None)

    resp = client.get("/api/projects/p1/story_memories", params={"chapter_id": "c1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    items = body["data"]["items"]
    assert len(items) == 1
    assert items[0]["id"] == "m1"
    assert items[0]["chapter_id"] == "c1"


# ====================================================================
# story_memory.py —— POST /api/projects/{project_id}/story_memories
# ====================================================================


def test_create_story_memory_returns_and_persists() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)

    resp = client.post(
        "/api/projects/p1/story_memories",
        json={
            "memory_type": "event",
            "title": "Created memory",
            "content": "新创建的剧情记忆",
            "importance_score": 2.5,
            "tags": ["plot", "key-plot"],
            "story_timeline": 3,
            "is_foreshadow": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    sm = body["data"]["story_memory"]
    assert sm["id"]
    assert sm["project_id"] == "p1"
    assert sm["memory_type"] == "event"
    assert sm["title"] == "Created memory"
    assert sm["content"] == "新创建的剧情记忆"
    assert sm["importance_score"] == 2.5
    assert sm["story_timeline"] == 3
    assert sm["is_foreshadow"] is True
    assert sm["tags"] == ["plot", "key-plot"]
    assert sm["done"] is False

    # 回读验证持久化
    new_id = sm["id"]
    with factory() as db:
        row = db.get(StoryMemory, new_id)
        assert row is not None
        assert row.title == "Created memory"
        assert row.importance_score == 2.5


# ====================================================================
# story_memory.py —— PUT /api/projects/{project_id}/story_memories/{id}
# ====================================================================


def test_update_story_memory_fields() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    create_resp = client.post(
        "/api/projects/p1/story_memories",
        json={"memory_type": "event", "content": "原始内容"},
    )
    assert create_resp.status_code == 200
    memory_id = create_resp.json()["data"]["story_memory"]["id"]

    resp = client.put(
        f"/api/projects/p1/story_memories/{memory_id}",
        json={
            "title": "更新标题",
            "content": "更新后的内容",
            "importance_score": 9.0,
            "tags": ["revised"],
            "is_foreshadow": False,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    sm = body["data"]["story_memory"]
    assert sm["id"] == memory_id
    assert sm["title"] == "更新标题"
    assert sm["content"] == "更新后的内容"
    assert sm["importance_score"] == 9.0
    assert sm["tags"] == ["revised"]
    assert sm["is_foreshadow"] is False


# ====================================================================
# story_memory.py —— DELETE /api/projects/{project_id}/story_memories/{id}
# ====================================================================


def test_delete_story_memory_returns_ok_and_removes() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    create_resp = client.post(
        "/api/projects/p1/story_memories",
        json={"memory_type": "event", "content": "待删除"},
    )
    assert create_resp.status_code == 200
    memory_id = create_resp.json()["data"]["story_memory"]["id"]

    resp = client.delete(f"/api/projects/p1/story_memories/{memory_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {"deleted_id": memory_id}

    # 回读验证已删除
    with factory() as db:
        assert db.get(StoryMemory, memory_id) is None


# ====================================================================
# story_memory.py —— POST /api/projects/{project_id}/story_memories/merge
# ====================================================================


def test_merge_story_memories_combines_content_and_deletes_source() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)

    target_resp = client.post(
        "/api/projects/p1/story_memories",
        json={"memory_type": "event", "content": "目标记忆正文", "importance_score": 1.0},
    )
    assert target_resp.status_code == 200
    target_id = target_resp.json()["data"]["story_memory"]["id"]

    source_resp = client.post(
        "/api/projects/p1/story_memories",
        json={"memory_type": "event", "content": "来源记忆正文", "importance_score": 4.0},
    )
    assert source_resp.status_code == 200
    source_id = source_resp.json()["data"]["story_memory"]["id"]

    resp = client.post(
        "/api/projects/p1/story_memories/merge",
        json={"target_id": target_id, "source_ids": [source_id]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["deleted_ids"] == [source_id]
    merged = data["story_memory"]
    assert "目标记忆正文" in merged["content"]
    assert "来源记忆正文" in merged["content"]
    # importance 取最大值
    assert merged["importance_score"] == 4.0

    # 回读：源已删除，目标仍存在
    with factory() as db:
        assert db.get(StoryMemory, source_id) is None
        assert db.get(StoryMemory, target_id) is not None


# ====================================================================
# story_memory.py —— POST .../mark_done
# ====================================================================


def test_mark_done_story_memory_sets_done_flag() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    create_resp = client.post(
        "/api/projects/p1/story_memories",
        json={"memory_type": "event", "content": "待标记完成"},
    )
    assert create_resp.status_code == 200
    memory_id = create_resp.json()["data"]["story_memory"]["id"]
    assert create_resp.json()["data"]["story_memory"]["done"] is False

    resp = client.post(
        f"/api/projects/p1/story_memories/{memory_id}/mark_done",
        json={"done": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["story_memory"]["done"] is True
    assert body["data"]["story_memory"]["id"] == memory_id


# ====================================================================
# memory.py —— POST /api/projects/{project_id}/story_memories/import_all
# ====================================================================


def test_import_all_story_memories_creates_rows() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)

    resp = client.post(
        "/api/projects/p1/story_memories/import_all",
        json={
            "schema_version": "story_memory_import_v1",
            "memories": [
                {
                    "memory_type": "event",
                    "title": "导入条目一",
                    "content": "第一条导入内容",
                    "importance_score": 2.0,
                    "story_timeline": 1,
                },
                {
                    "memory_type": "lore",
                    "title": "导入条目二",
                    "content": "第二条导入内容",
                    "importance_score": 1.0,
                },
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["created"] == 2
    assert len(data["ids"]) == 2

    # 通过 list 端点回读验证
    list_resp = client.get("/api/projects/p1/story_memories")
    assert list_resp.status_code == 200
    items = list_resp.json()["data"]["items"]
    assert len(items) == 2
