"""export/import_export 域路由 happy-path（D 类安全网）。

覆盖 ``app/api/routes/export.py`` 与 ``app/api/routes/import_export.py``
的主要 HTTP 端点：

export.py:
  GET /api/projects/{project_id}/export/markdown
  GET /api/projects/{project_id}/export/bundle

import_export.py:
  GET  /api/projects/{project_id}/imports
  POST /api/projects/{project_id}/imports
  GET  /api/projects/{project_id}/imports/{document_id}
  GET  /api/projects/{project_id}/imports/{document_id}/chunks
  POST /api/projects/{project_id}/imports/{document_id}/retry

这些都是当前正确的活功能，测试应保持绿，构成导出/导入域的安全网。

跳过项：
  - bundle 导入端点（POST /api/projects/import_bundle）位于 projects.py，
    不在本文件作用域内；其服务层 roundtrip 已由 test_project_bundle_roundtrip.py
    覆盖。
"""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import select

from app.api.routes import export as export_routes
from app.api.routes import import_export as import_export_routes
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.knowledge_base import KnowledgeBase
from app.models.llm_preset import LLMPreset
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_membership import ProjectMembership
from app.models.project_settings import ProjectSettings
from app.models.project_source_document import ProjectSourceDocument, ProjectSourceDocumentChunk
from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
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


# export/bundle 路径会联查 settings/outline/chapter/character/llm_preset/
# prompt_preset/prompt_block/story_memory/knowledge_bases/source_documents；
# import_export 路径会读写 project_source_documents(_chunks) 与 knowledge_bases。
# 一次性建齐避免逐用例补建。
_MODELS = [
    User,
    UserPassword,
    Project,
    ProjectMembership,
    ProjectSettings,
    Outline,
    Chapter,
    Character,
    LLMPreset,
    PromptPreset,
    PromptBlock,
    StoryMemory,
    KnowledgeBase,
    ProjectSourceDocument,
    ProjectSourceDocumentChunk,
]


class _DummyQueue:
    """TaskQueue 测试桩：记录 enqueue 调用，不在后台真正执行任务。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def enqueue(self, *, kind: str, task_id: str) -> str:
        self.calls.append((kind, task_id))
        return task_id

    def enqueue_batch_generation_task(self, task_id: str) -> str:
        return self.enqueue(kind="batch_generation", task_id=task_id)


def _new_client_and_factory():
    """构造一个已建表 + 已登录 u1 并挂载 export/import_export 路由的测试 client。"""
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, _MODELS)
    seed_user(factory, user_id="u1")
    app = make_test_app(factory, [export_routes, import_export_routes])
    client = make_client(app)
    auth_cookies(client, "u1")
    return client, factory


def _seed_project(factory, *, project_id: str = "p1", name: str = "P1") -> str:
    with factory() as db:
        db.add(Project(id=project_id, owner_user_id="u1", name=name))
        db.add(ProjectMembership(project_id=project_id, user_id="u1", role="owner"))
        db.commit()
    return project_id


def _seed_full_project(factory) -> str:
    """导出测试用：项目 + 设定 + 大纲 + 章节 + 角色，并激活大纲。"""
    with factory() as db:
        project = Project(id="p1", owner_user_id="u1", name="My Novel", genre="scifi", logline="A logline.")
        db.add(project)
        db.add(ProjectMembership(project_id="p1", user_id="u1", role="owner"))
        db.add(
            ProjectSettings(
                project_id="p1",
                world_setting="World setup",
                style_guide="Style guide",
                constraints="Constraints text",
            )
        )
        db.add(Outline(id="o1", project_id="p1", title="Outline 1", content_md="# Outline body"))
        db.add(
            Chapter(
                id="c1",
                project_id="p1",
                outline_id="o1",
                number=1,
                title="Chapter One",
                content_md="Chapter body content.",
                status="done",
            )
        )
        db.add(Character(id="char1", project_id="p1", name="Alice", role="hero", profile="A brave hero."))
        project.active_outline_id = "o1"
        db.commit()
    return "p1"


# ---------- GET /api/projects/{project_id}/export/markdown ----------


def test_export_markdown_minimal_project_returns_ok_with_download_headers() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, name="Bare Project")
    resp = client.get("/api/projects/p1/export/markdown")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert ".md" in disposition
    body = resp.text
    # 项目名出现在标题中（不锁中文段头文案）
    assert "Bare Project" in body


def test_export_markdown_full_project_returns_ok() -> None:
    client, factory = _new_client_and_factory()
    _seed_full_project(factory)
    resp = client.get("/api/projects/p1/export/markdown")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "attachment" in resp.headers["content-disposition"]
    body = resp.text
    assert "My Novel" in body
    # 大纲与章节正文应进入导出（不锁中文小节标题，只断言结构化内容存在）
    assert "# Outline body" in body
    assert "Chapter body content." in body
    assert "Alice" in body


# ---------- GET /api/projects/{project_id}/export/bundle ----------


def test_export_bundle_returns_bundle_structure_with_download_headers() -> None:
    client, factory = _new_client_and_factory()
    _seed_full_project(factory)
    resp = client.get("/api/projects/p1/export/bundle")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert ".bundle.json" in disposition

    bundle = resp.json()
    # 顶层结构关键键（不锁文案）
    expected_top_keys = {
        "schema_version",
        "exported_at",
        "project",
        "settings",
        "llm_preset",
        "outlines",
        "chapters",
        "characters",
        "prompt_presets",
        "story_memory",
        "knowledge_bases",
        "source_documents",
    }
    assert expected_top_keys.issubset(bundle.keys())
    assert bundle["schema_version"] == "project_bundle_v1"
    assert bundle["project"]["id"] == "p1"
    assert bundle["project"]["name"] == "My Novel"
    assert bundle["project"]["genre"] == "scifi"
    # 子集合内容真实落库
    assert len(bundle["outlines"]) == 1
    assert bundle["outlines"][0]["id"] == "o1"
    assert len(bundle["chapters"]) == 1
    assert bundle["chapters"][0]["id"] == "c1"
    assert len(bundle["characters"]) == 1
    assert bundle["characters"][0]["name"] == "Alice"
    # settings 子结构存在（不锁具体文案）
    assert "world_setting" in bundle["settings"]
    assert "vector_embedding" in bundle["settings"]


def test_export_bundle_empty_project_returns_valid_empty_bundle() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, name="Empty")
    resp = client.get("/api/projects/p1/export/bundle")
    assert resp.status_code == 200
    bundle = resp.json()
    assert bundle["schema_version"] == "project_bundle_v1"
    assert bundle["project"]["id"] == "p1"
    # 空项目：各集合为空列表/空结构，但键必须存在
    assert bundle["outlines"] == []
    assert bundle["chapters"] == []
    assert bundle["characters"] == []
    assert bundle["settings"]["world_setting"] == ""
    assert bundle["llm_preset"] is None


# ---------- GET /api/projects/{project_id}/imports ----------


def test_list_imports_empty_returns_empty_documents() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    resp = client.get("/api/projects/p1/imports")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {"documents": []}


def test_list_imports_returns_seeded_documents() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    with factory() as db:
        db.add(
            ProjectSourceDocument(
                id="doc1",
                project_id="p1",
                actor_user_id="u1",
                filename="notes.txt",
                content_type="txt",
                content_text="hello world",
                status="done",
                progress=100,
                progress_message="done",
                chunk_count=0,
                kb_id="default",
            )
        )
        db.commit()
    resp = client.get("/api/projects/p1/imports")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    docs = body["data"]["documents"]
    assert len(docs) == 1
    doc = docs[0]
    expected_keys = {
        "id",
        "project_id",
        "actor_user_id",
        "filename",
        "content_type",
        "status",
        "progress",
        "progress_message",
        "chunk_count",
        "kb_id",
        "error_message",
        "created_at",
        "updated_at",
    }
    assert expected_keys.issubset(doc.keys())
    assert doc["id"] == "doc1"
    assert doc["project_id"] == "p1"
    assert doc["filename"] == "notes.txt"
    assert doc["content_type"] == "txt"
    assert doc["status"] == "done"


# ---------- GET /api/projects/{project_id}/imports/{document_id} ----------


def test_get_import_returns_document_detail_with_previews() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    with factory() as db:
        db.add(
            ProjectSourceDocument(
                id="doc1",
                project_id="p1",
                actor_user_id="u1",
                filename="long.txt",
                content_type="txt",
                content_text="line one\nline two\nline three",
                status="done",
                progress=100,
                progress_message="done",
                chunk_count=2,
                kb_id="default",
            )
        )
        db.commit()
    resp = client.get("/api/projects/p1/imports/doc1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    expected_keys = {"document", "content_preview", "vector_ingest_result", "story_memory_proposal"}
    assert expected_keys.issubset(data.keys())
    assert data["document"]["id"] == "doc1"
    assert data["document"]["filename"] == "long.txt"
    assert isinstance(data["content_preview"], str)
    assert isinstance(data["vector_ingest_result"], dict)
    assert isinstance(data["story_memory_proposal"], dict)


# ---------- GET /api/projects/{project_id}/imports/{document_id}/chunks ----------


def test_list_import_chunks_returns_chunks_with_count() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    with factory() as db:
        db.add(
            ProjectSourceDocument(
                id="doc1",
                project_id="p1",
                actor_user_id="u1",
                filename="doc.txt",
                content_type="txt",
                content_text="abc",
                status="done",
                progress=100,
                progress_message="done",
                chunk_count=2,
                kb_id="default",
            )
        )
        db.add(
            ProjectSourceDocumentChunk(
                id="ck1",
                document_id="doc1",
                chunk_index=0,
                content_text="first chunk text here",
                vector_chunk_id="source_doc:doc1:0",
            )
        )
        db.add(
            ProjectSourceDocumentChunk(
                id="ck2",
                document_id="doc1",
                chunk_index=1,
                content_text="second chunk text here",
                vector_chunk_id="source_doc:doc1:1",
            )
        )
        db.commit()
    resp = client.get("/api/projects/p1/imports/doc1/chunks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    chunks = body["data"]["chunks"]
    assert body["data"]["returned"] == 2
    assert len(chunks) == 2
    expected_keys = {"id", "chunk_index", "preview", "vector_chunk_id"}
    assert expected_keys.issubset(chunks[0].keys())
    assert chunks[0]["chunk_index"] == 0
    assert chunks[1]["chunk_index"] == 1
    assert chunks[0]["vector_chunk_id"] == "source_doc:doc1:0"


# ---------- POST /api/projects/{project_id}/imports ----------


def test_create_import_creates_document_and_returns_job_id() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    dummy_queue = _DummyQueue()
    with patch("app.api.routes.import_export.get_task_queue", return_value=dummy_queue):
        resp = client.post(
            "/api/projects/p1/imports",
            json={"filename": "story.md", "content_text": "Once upon a time."},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert "document" in data
    assert "job_id" in data
    doc = data["document"]
    assert doc["project_id"] == "p1"
    assert doc["actor_user_id"] == "u1"
    assert doc["filename"] == "story.md"
    # content_type 由扩展名推断
    assert doc["content_type"] == "md"
    assert doc["status"] == "queued"
    # 已入队一次
    assert len(dummy_queue.calls) == 1
    assert dummy_queue.calls[0][0] == "import_task"
    # 真实落库可回读
    with factory() as db:
        row = db.get(ProjectSourceDocument, doc["id"])
        assert row is not None
        assert row.filename == "story.md"
        assert row.content_text == "Once upon a time."


def test_create_import_defaults_content_type_to_txt_for_unknown_extension() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    with patch("app.api.routes.import_export.get_task_queue", return_value=_DummyQueue()):
        resp = client.post(
            "/api/projects/p1/imports",
            json={"filename": "data.xyz", "content_text": "payload"},
        )
    assert resp.status_code == 200
    doc = resp.json()["data"]["document"]
    assert doc["content_type"] == "txt"


# ---------- POST /api/projects/{project_id}/imports/{document_id}/retry ----------


def test_retry_import_returns_ok_with_cleanup_and_resets_doc() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    with factory() as db:
        db.add(
            ProjectSourceDocument(
                id="doc1",
                project_id="p1",
                actor_user_id="u1",
                filename="retry.txt",
                content_type="txt",
                content_text="retry me",
                status="failed",
                progress=0,
                progress_message="failed",
                chunk_count=1,
                kb_id="kb1",
                error_message="previous_error",
            )
        )
        db.add(
            ProjectSourceDocumentChunk(
                id="ck1",
                document_id="doc1",
                chunk_index=0,
                content_text="old chunk",
                vector_chunk_id="source_doc:doc1:0",
            )
        )
        db.commit()

    dummy_queue = _DummyQueue()
    # retry_import_task 内部使用 SessionLocal() 直连；patch 为测试 factory 使其命中测试库。
    # 路由内 get_task_queue 也 patch 掉，避免触发后台 worker。
    with patch("app.services.import_export_service.SessionLocal", factory), patch(
        "app.api.routes.import_export.get_task_queue", return_value=dummy_queue
    ):
        resp = client.post("/api/projects/p1/imports/doc1/retry")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    expected_keys = {"document", "cleanup", "job_id", "enqueue_error"}
    assert expected_keys.issubset(data.keys())
    assert data["document"]["id"] == "doc1"
    # cleanup 命中测试库（patch 生效），成功清理
    assert isinstance(data["cleanup"], dict)
    assert data["cleanup"].get("ok") is True
    # 重新入队一次
    assert len(dummy_queue.calls) == 1
    assert dummy_queue.calls[0][0] == "import_task"
    assert data["enqueue_error"] is None
    # 真实副作用：旧 chunk 被清理、状态被重置为 queued
    with factory() as db:
        chunks = (
            db.execute(
                select(ProjectSourceDocumentChunk).where(
                    ProjectSourceDocumentChunk.document_id == "doc1"
                )
            )
            .scalars()
            .all()
        )
        assert len(chunks) == 0
        row = db.get(ProjectSourceDocument, "doc1")
        assert row is not None
        assert row.status == "queued"
        assert row.error_message is None
