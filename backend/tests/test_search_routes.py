"""search 域路由 happy-path（D 类安全网）。

覆盖 ``app/api/routes/search.py`` 的端点：

  POST /api/projects/{project_id}/search/query

search 当前实现对 chapters/outlines/characters/entries/story_memories/
source_documents 等实体表做全表 LIKE 匹配（见 ``query_project_search`` 的
``mode="direct"`` 分支），绕过自维护 FTS 索引。本文件只覆盖 happy-path：
seed 真实实体 + 已知关键字，调端点，断言【响应结构】（ok=True、data 关键键
items/mode/next_offset/fts_enabled、item 形状 source_type/source_id/title/
snippet/jump_url/locator_json），不断言用的是 FTS 还是 LIKE——那是 known_issue
的范畴（见 test_search_fuzzy_query.py / test_search_query_endpoint.py，共存不冲突）。

跳过项：无 —— search 路由仅此一个端点，所有 happy 路径都在本文件覆盖。
"""

from __future__ import annotations

from app.api.routes import search as search_routes
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.entry import Entry
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_membership import ProjectMembership
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


PROJECT_ID = "p1"
USER_ID = "u1"

# search 路径触及的实体表 + 认证/项目归属表。query_project_search 无条件扫
# chapters/characters/outlines/entries/story_memories（story_memories 未做
# _has_table 守卫，必须建表），Chapter.outline_id 是 NOT NULL FK 故 Outline 必建。
_MODELS = [
    User,
    UserPassword,
    Project,
    ProjectMembership,
    Outline,
    Chapter,
    Character,
    Entry,
    StoryMemory,
]


def _new_client_and_factory():
    """构造一个已建表 + 已登录 u1 的测试 client 与 session factory。"""
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, _MODELS)
    seed_user(factory, user_id=USER_ID)
    app = make_test_app(factory, [search_routes])
    client = make_client(app)
    auth_cookies(client, USER_ID)
    return client, factory


def _seed_project(factory, *, project_id: str = PROJECT_ID, owner_user_id: str = USER_ID, name: str = "P1") -> str:
    """直接在 DB 插入一个 Project + owner membership，返回 project_id。"""
    with factory() as db:
        db.add(Project(id=project_id, owner_user_id=owner_user_id, name=name))
        db.add(ProjectMembership(project_id=project_id, user_id=owner_user_id, role="owner"))
        db.commit()
    return project_id


def _seed_chapter(
    factory,
    *,
    chapter_id: str,
    number: int,
    title: str,
    content_md: str,
    summary: str | None = None,
) -> str:
    """插入一个 outline + chapter，返回 chapter_id（chapter.outline_id 是 NOT NULL FK）。"""
    outline_id = f"{chapter_id}-outline"
    with factory() as db:
        db.add(Outline(id=outline_id, project_id=PROJECT_ID, title=f"{chapter_id}-outline"))
        db.add(
            Chapter(
                id=chapter_id,
                project_id=PROJECT_ID,
                outline_id=outline_id,
                number=number,
                title=title,
                content_md=content_md,
                summary=summary,
            )
        )
        db.commit()
    return chapter_id


# ---------- POST /api/projects/{project_id}/search/query ----------


def test_search_empty_query_returns_empty_items_with_empty_mode() -> None:
    """空 q 返回 items=[] 且 mode="empty"。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    resp = client.post(f"/api/projects/{PROJECT_ID}/search/query", json={"q": "", "limit": 20, "offset": 0})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["items"] == []
    assert data["mode"] == "empty"
    assert data["next_offset"] is None
    assert data["fts_enabled"] is False


def test_search_no_match_returns_empty_items_with_direct_mode() -> None:
    """有 q 但无匹配 → items=[] 且 mode="direct"。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/search/query",
        json={"q": "完全不存在的关键字Zyx", "limit": 20, "offset": 0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["items"] == []
    assert data["mode"] == "direct"
    assert data["next_offset"] is None


def test_search_finds_chapter_by_content_md() -> None:
    """seed 一个 chapter 含已知关键字，搜索命中并返回结构化 item。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    _seed_chapter(
        factory,
        chapter_id="c1",
        number=1,
        title="起点",
        content_md="独特关键词Alpha出现在章节正文里",
    )
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/search/query",
        json={"q": "独特关键词Alpha", "limit": 20, "offset": 0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    items = body["data"]["items"]
    assert len(items) == 1
    item = items[0]
    # 断言结构关键键，不锁中文文案
    expected_keys = {"source_type", "source_id", "title", "snippet", "jump_url", "locator_json"}
    assert expected_keys.issubset(item.keys())
    assert item["source_type"] == "chapter"
    assert item["source_id"] == "c1"
    assert item["jump_url"].startswith(f"/projects/{PROJECT_ID}/writing")
    assert item["snippet"]  # 非空


def test_search_finds_character_by_name() -> None:
    """seed 一个 character，按 name 关键字命中。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    with factory() as db:
        db.add(Character(id="char1", project_id=PROJECT_ID, name="独特角色Beta"))
        db.commit()
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/search/query",
        json={"q": "独特角色Beta", "limit": 20, "offset": 0},
    )
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    item = items[0]
    assert item["source_type"] == "character"
    assert item["source_id"] == "char1"
    assert item["jump_url"] == f"/projects/{PROJECT_ID}/characters"


def test_search_finds_entry_by_title() -> None:
    """seed 一个 entry，按 title 关键字命中。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    with factory() as db:
        db.add(Entry(id="e1", project_id=PROJECT_ID, title="独特条目Gamma", content=""))
        db.commit()
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/search/query",
        json={"q": "独特条目Gamma", "limit": 20, "offset": 0},
    )
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    item = items[0]
    assert item["source_type"] == "entry"
    assert item["source_id"] == "e1"
    assert item["jump_url"] == f"/projects/{PROJECT_ID}/entries"


def test_search_finds_outline_by_title() -> None:
    """seed 一个 outline，按 title 关键字命中。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    with factory() as db:
        db.add(Outline(id="o1", project_id=PROJECT_ID, title="独特大纲Delta"))
        db.commit()
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/search/query",
        json={"q": "独特大纲Delta", "limit": 20, "offset": 0},
    )
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    item = items[0]
    assert item["source_type"] == "outline"
    assert item["source_id"] == "o1"
    assert item["jump_url"] == f"/projects/{PROJECT_ID}/outline"


def test_search_source_filter_restricts_to_chapter_only() -> None:
    """sources=["chapter"] 时，即使其它实体也匹配关键字，也只返回 chapter。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    # 同一关键字 "OmegaKey" 同时出现在 chapter 和 entry
    _seed_chapter(
        factory,
        chapter_id="c1",
        number=1,
        title="OmegaKey 标题",
        content_md="OmegaKey 正文",
    )
    with factory() as db:
        db.add(Entry(id="e1", project_id=PROJECT_ID, title="OmegaKey 条目", content=""))
        db.commit()
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/search/query",
        json={"q": "OmegaKey", "sources": ["chapter"], "limit": 20, "offset": 0},
    )
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["source_type"] == "chapter"
    assert items[0]["source_id"] == "c1"


def test_search_pagination_returns_next_offset_when_more_available() -> None:
    """limit=1 + 多条匹配 → 返回 1 条且 next_offset=1；翻页后 next_offset=None。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    with factory() as db:
        db.add(Character(id="char1", project_id=PROJECT_ID, name="FinderEcho-1"))
        db.add(Character(id="char2", project_id=PROJECT_ID, name="FinderEcho-2"))
        db.commit()
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/search/query",
        json={"q": "FinderEcho", "limit": 1, "offset": 0},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["items"]) == 1
    assert data["next_offset"] == 1
    # 第二页：只剩 1 条，无更多
    resp2 = client.post(
        f"/api/projects/{PROJECT_ID}/search/query",
        json={"q": "FinderEcho", "limit": 1, "offset": 1},
    )
    data2 = resp2.json()["data"]
    assert len(data2["items"]) == 1
    assert data2["next_offset"] is None


def test_search_with_default_limit_offset_from_schema() -> None:
    """不传 limit/offset 也能正常工作（走 SearchQueryRequest 的 schema 默认值）。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory)
    _seed_chapter(
        factory,
        chapter_id="c1",
        number=1,
        title="独有词条Zeta",
        content_md="内容",
    )
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/search/query",
        json={"q": "独有词条Zeta"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert len(body["data"]["items"]) == 1
