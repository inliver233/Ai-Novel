from __future__ import annotations

import unittest
from datetime import datetime, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.entry import Entry
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_source_document import ProjectSourceDocument
from app.models.story_memory import StoryMemory
from app.models.user import User
from app.services.search_index_service import query_project_search


class TestSearchFuzzyQuery(unittest.TestCase):
    """验证项目搜索的可观察语义，不锁定当前 ``direct``/LIKE 实现。

    - 多个查询词按 AND 子串语义匹配（非连续词亦可命中）；
    - 命中返回结构完整的 item（source_type/source_id/snippet/jump_url）；
    - 标题命中的结果排在内容命中之前（title_hit 排序）。

    H15 的无界候选查询由独立数据库边界测试约束；未来切换到
    search_documents/FTS 时这些功能语义测试不应阻碍重构。
    """

    def _make_db(self) -> sessionmaker:
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(
            engine,
            tables=[
                User.__table__,
                Project.__table__,
                Outline.__table__,
                Chapter.__table__,
                Character.__table__,
                StoryMemory.__table__,
                ProjectSourceDocument.__table__,
                Entry.__table__,
            ],
        )
        return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _candidate_search_queries(self, statements: list[str]) -> list[str]:
        """Return candidate-search SELECTs without tying the test to a table or backend."""

        normalized = [" ".join(sql.lower().split()) for sql in statements]
        return [
            sql
            for sql in normalized
            if (sql.startswith("select ") or sql.startswith("with "))
            and any(operator in f" {sql} " for operator in (" like ", " match ", " @@ "))
        ]

    def _assert_candidate_searches_are_bounded(self, statements: list[str]) -> None:
        candidate_queries = self._candidate_search_queries(statements)
        self.assertTrue(candidate_queries, "search must execute at least one database candidate query")
        unbounded_queries = [sql for sql in candidate_queries if " limit " not in f" {sql} "]
        self.assertFalse(
            unbounded_queries,
            f"candidate search queries must apply a database LIMIT: {unbounded_queries}",
        )

    def test_fuzzy_matches_non_contiguous_terms(self) -> None:
        # 非连续查询词（"Hello world"）应命中内容 "Hello brave world" 的章节。
        SessionLocal = self._make_db()
        with SessionLocal() as db:
            db.add(User(id="u1", display_name="u1"))
            db.add(Project(id="p1", owner_user_id="u1", name="p1"))
            db.add(Outline(id="o1", project_id="p1", title="大纲"))
            db.add(
                Chapter(
                    id="c1",
                    project_id="p1",
                    outline_id="o1",
                    number=1,
                    title="Start",
                    content_md="Hello brave world",
                )
            )
            db.commit()

            out = query_project_search(
                db=db, project_id="p1", q="Hello world", sources=["chapter"], limit=20, offset=0
            )
            items = out.get("items") or []
            self.assertTrue(items)
            self.assertEqual(items[0].get("source_id"), "c1")

    def test_result_fields_for_non_contiguous_match(self) -> None:
        # 非连续查询词命中章节，且返回的 item 字段结构完整。
        SessionLocal = self._make_db()
        with SessionLocal() as db:
            db.add(User(id="u1", display_name="u1"))
            db.add(Project(id="p1", owner_user_id="u1", name="p1"))
            db.add(Outline(id="o1", project_id="p1", title="大纲"))
            db.add(
                Chapter(
                    id="c1",
                    project_id="p1",
                    outline_id="o1",
                    number=1,
                    title="Title",
                    content_md="alpha brave beta",
                )
            )
            db.commit()

            out = query_project_search(
                db=db, project_id="p1", q="alpha beta", sources=["chapter"], limit=20, offset=0
            )
            items = out.get("items") or []
            self.assertTrue(items)
            item = items[0]
            self.assertEqual(item.get("source_type"), "chapter")
            self.assertEqual(item.get("source_id"), "c1")
            self.assertIsNotNone(item.get("snippet"))
            self.assertTrue(item.get("jump_url"))

    def test_order_prefers_title_match(self) -> None:
        # 标题命中（title_hit=0）的章节应排在仅内容命中（title_hit=1）的章节之前。
        SessionLocal = self._make_db()
        with SessionLocal() as db:
            db.add(User(id="u1", display_name="u1"))
            db.add(Project(id="p1", owner_user_id="u1", name="p1"))
            db.add(Outline(id="o1", project_id="p1", title="大纲"))
            db.add(
                Chapter(
                    id="a",
                    project_id="p1",
                    outline_id="o1",
                    number=1,
                    title="Hello world",
                    content_md="",
                )
            )
            db.add(
                Chapter(
                    id="b",
                    project_id="p1",
                    outline_id="o1",
                    number=2,
                    title="Something else",
                    content_md="Hello world",
                )
            )
            db.commit()

            out = query_project_search(
                db=db, project_id="p1", q="Hello world", sources=["chapter"], limit=20, offset=0
            )
            items = out.get("items") or []
            self.assertGreaterEqual(len(items), 2)
            self.assertEqual(items[0].get("source_id"), "a")

    def test_candidate_query_applies_database_limit(self) -> None:
        SessionLocal = self._make_db()
        engine = SessionLocal.kw["bind"]
        with SessionLocal() as db:
            db.add(User(id="u1", display_name="u1"))
            db.add(Project(id="p1", owner_user_id="u1", name="p1"))
            db.add(Outline(id="o1", project_id="p1", title="outline"))
            for number in range(1, 6):
                db.add(
                    Chapter(
                        id=f"c{number}",
                        project_id="p1",
                        outline_id="o1",
                        number=number,
                        title=f"match-{number}",
                        content_md="shared match body",
                    )
                )
            db.commit()

            statements: list[str] = []

            def _capture(_conn, _cursor, statement, _params, _context, _executemany):  # type: ignore[no-untyped-def]
                statements.append(str(statement).lower())

            event.listen(engine, "before_cursor_execute", _capture)
            try:
                out = query_project_search(
                    db=db,
                    project_id="p1",
                    q="match",
                    sources=["chapter"],
                    limit=1,
                    offset=0,
                )
            finally:
                event.remove(engine, "before_cursor_execute", _capture)

        self.assertEqual(len(out.get("items") or []), 1)
        self.assertEqual(out.get("next_offset"), 1)
        # Only candidate searches (LIKE/MATCH/full-text predicate) must be bounded. A later
        # hydration SELECT already constrained by candidate IDs need not repeat LIMIT.
        self._assert_candidate_searches_are_bounded(statements)

    def test_all_supported_sources_apply_bounded_candidate_queries(self) -> None:
        SessionLocal = self._make_db()
        engine = SessionLocal.kw["bind"]
        with SessionLocal() as db:
            db.add(User(id="u1", display_name="u1"))
            db.add(Project(id="p1", owner_user_id="u1", name="p1"))
            db.add(Outline(id="o1", project_id="p1", title="bounded-outline"))
            db.add(
                Chapter(
                    id="c1",
                    project_id="p1",
                    outline_id="o1",
                    number=1,
                    title="bounded-chapter",
                    content_md="body",
                )
            )
            db.add(Character(id="character1", project_id="p1", name="bounded-character"))
            db.add(
                StoryMemory(
                    id="memory1",
                    project_id="p1",
                    chapter_id="c1",
                    memory_type="event",
                    title="bounded-memory",
                    content="body",
                )
            )
            db.add(
                ProjectSourceDocument(
                    id="document1",
                    project_id="p1",
                    actor_user_id="u1",
                    filename="bounded-document.txt",
                    content_text="body",
                )
            )
            db.add(Entry(id="entry1", project_id="p1", title="bounded-entry", content="body"))
            db.commit()

            source_ids = {
                "chapter": "c1",
                "outline": "o1",
                "character": "character1",
                "story_memory": "memory1",
                "source_document": "document1",
                "entry": "entry1",
            }
            statements: list[str] = []

            def _capture(_conn, _cursor, statement, _params, _context, _executemany):  # type: ignore[no-untyped-def]
                statements.append(str(statement))

            event.listen(engine, "before_cursor_execute", _capture)
            try:
                for source, source_id in source_ids.items():
                    with self.subTest(source=source):
                        statements.clear()
                        out = query_project_search(
                            db=db,
                            project_id="p1",
                            q="bounded",
                            sources=[source],
                            limit=1,
                            offset=0,
                        )
                        items = out.get("items") or []
                        self.assertEqual([item.get("source_id") for item in items], [source_id])
                        self._assert_candidate_searches_are_bounded(statements)
            finally:
                event.remove(engine, "before_cursor_execute", _capture)

    def test_older_title_hit_outranks_newer_body_hits_with_small_limit(self) -> None:
        SessionLocal = self._make_db()
        with SessionLocal() as db:
            db.add(User(id="u1", display_name="u1"))
            db.add(Project(id="p1", owner_user_id="u1", name="p1"))
            db.add(Outline(id="o1", project_id="p1", title="outline"))
            db.add(
                Chapter(
                    id="title-hit",
                    project_id="p1",
                    outline_id="o1",
                    number=1,
                    title="needle in title",
                    content_md="body",
                    updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                )
            )
            for number in range(2, 6):
                db.add(
                    Chapter(
                        id=f"body-hit-{number}",
                        project_id="p1",
                        outline_id="o1",
                        number=number,
                        title=f"newer chapter {number}",
                        content_md="needle in body",
                        updated_at=datetime(2024, 1, number, tzinfo=timezone.utc),
                    )
                )
            db.commit()

            out = query_project_search(
                db=db,
                project_id="p1",
                q="needle",
                sources=["chapter"],
                limit=1,
                offset=0,
            )

        items = out.get("items") or []
        self.assertEqual([item.get("source_id") for item in items], ["title-hit"])
        self.assertEqual(out.get("next_offset"), 1)

    def test_deep_offset_keeps_enough_database_candidates_for_the_page(self) -> None:
        SessionLocal = self._make_db()
        with SessionLocal() as db:
            db.add(User(id="u1", display_name="u1"))
            db.add(Project(id="p1", owner_user_id="u1", name="p1"))
            db.add(Outline(id="o1", project_id="p1", title="outline"))
            for number in range(1, 9):
                db.add(
                    Chapter(
                        id=f"c{number}",
                        project_id="p1",
                        outline_id="o1",
                        number=number,
                        title=f"needle-{number}",
                        content_md="body",
                        updated_at=datetime(2024, 1, number, tzinfo=timezone.utc),
                    )
                )
            db.commit()

            out = query_project_search(
                db=db,
                project_id="p1",
                q="needle",
                sources=["chapter"],
                limit=2,
                offset=5,
            )

        items = out.get("items") or []
        self.assertEqual([item.get("source_id") for item in items], ["c3", "c2"])
        self.assertEqual(out.get("next_offset"), 7)
