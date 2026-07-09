from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.chapter import Chapter
from app.models.outline import Outline
from app.models.project import Project
from app.models.user import User
from app.services.search_index_service import query_project_search


class TestSearchFuzzyQuery(unittest.TestCase):
    """query_project_search 在业务表（chapters 等）上直接做 LIKE 检索，而非读取自维护的
    search_documents/FTS 索引——见 search_index_service.query_project_search 顶部 NOTE：
    “此路径有意不依赖 search_documents / FTS 表”。这里验证该路径的可观测行为：

    - 多个查询词按 AND 子串语义匹配（非连续词亦可命中）；
    - 命中返回结构完整的 item（source_type/source_id/snippet/jump_url）；
    - 标题命中的结果排在内容命中之前（title_hit 排序）。

    mode 仅可能为 none/empty/direct（不存在 "like"/"fts"）。
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

    def test_fts_fuzzy_matches_non_contiguous_terms(self) -> None:
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
            self.assertEqual(out.get("mode"), "direct")
            items = out.get("items") or []
            self.assertTrue(items)
            self.assertEqual(items[0].get("source_id"), "c1")

    def test_like_fuzzy_matches_non_contiguous_terms(self) -> None:
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
            self.assertEqual(out.get("mode"), "direct")
            items = out.get("items") or []
            self.assertTrue(items)
            item = items[0]
            self.assertEqual(item.get("source_type"), "chapter")
            self.assertEqual(item.get("source_id"), "c1")
            self.assertIsNotNone(item.get("snippet"))
            self.assertTrue(item.get("jump_url"))

    def test_like_order_prefers_title_match(self) -> None:
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
