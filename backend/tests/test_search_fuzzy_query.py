from __future__ import annotations

import unittest

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.chapter import Chapter
from app.models.outline import Outline
from app.models.project import Project
from app.models.user import User
from app.services.search_index_service import query_project_search


class TestSearchFuzzyQuery(unittest.TestCase):
    """验证项目搜索的可观察语义，不锁定当前 ``direct``/LIKE 实现。

    - 多个查询词按 AND 子串语义匹配（非连续词亦可命中）；
    - 命中返回结构完整的 item（source_type/source_id/snippet/jump_url）；
    - 标题命中的结果排在内容命中之前（title_hit 排序）。

    H15 的实现缺陷（无 LIMIT 全表 LIKE、维护的 FTS 只写不读）由独立 known_issue
    测试约束；未来切换到 search_documents/FTS 时这些功能语义测试不应阻碍重构。
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
            tables=[User.__table__, Project.__table__, Outline.__table__, Chapter.__table__],
        )
        return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

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

    @pytest.mark.known_issue  # H15/backend-rag-memory#6：数据库候选查询必须有界分页
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
        candidate_queries = [
            sql
            for sql in statements
            if sql.lstrip().startswith("select") and ("chapters" in sql or "search_documents" in sql)
        ]
        self.assertTrue(candidate_queries)
        # 正确行为：limit/offset 应下推数据库。当前实现把全部命中载入 Python 后再切片，
        # 候选 SELECT 无 LIMIT，故该断言真红。
        normalized_queries = [" ".join(sql.split()) for sql in candidate_queries]
        self.assertTrue(any(" limit " in f" {sql} " for sql in normalized_queries))
