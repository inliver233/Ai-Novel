from __future__ import annotations

import unittest

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_source_document import ProjectSourceDocument
from app.models.search_index import SearchDocument
from app.models.story_memory import StoryMemory
from app.models.user import User
from app.services import search_index_service


class TestSearchIndexService(unittest.TestCase):
    def test_rebuild_and_incremental_update_keeps_fts_in_sync(self) -> None:
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
                SearchDocument.__table__,
            ],
        )

        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE VIRTUAL TABLE search_index USING fts5("
                "title,content,"
                "content='search_documents',content_rowid='id',"
                "tokenize='unicode61'"
                ")"
            )

        SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        with SessionLocal() as db:
            db.add(User(id="u1", display_name="u1"))
            db.add(Project(id="p1", owner_user_id="u1", name="p1", genre=None, logline=None))
            db.add(Outline(id="o1", project_id="p1", title="Outline", content_md="Main plot", structure_json=None))
            db.add(
                Chapter(
                    id="c1",
                    project_id="p1",
                    outline_id="o1",
                    number=1,
                    title="Start",
                    plan=None,
                    content_md="Hello world",
                    summary=None,
                    status="done",
                )
            )
            db.add(Character(id="ch1", project_id="p1", name="Alice", role="hero", profile="Brave", notes=""))
            db.add(
                StoryMemory(
                    id="sm1",
                    project_id="p1",
                    chapter_id="c1",
                    memory_type="event",
                    title="相遇",
                    content="Alice meets Bob at UniqueStoryMemoryToken",
                    full_context_md=None,
                )
            )
            db.add(
                ProjectSourceDocument(
                    id="d1",
                    project_id="p1",
                    actor_user_id="u1",
                    filename="notes.txt",
                    content_type="txt",
                    content_text="UniqueDocToken",
                    status="done",
                    progress=100,
                    progress_message="",
                    chunk_count=0,
                    kb_id=None,
                    vector_ingest_result_json=None,
                    worldbook_proposal_json=None,
                    story_memory_proposal_json=None,
                    error_message=None,
                )
            )
            db.commit()

            result = search_index_service.rebuild_project_search_index(db=db, project_id="p1")
            db.commit()
            self.assertTrue(result.get("ok"))

            # 章节正文 / 源文档 / 故事记忆三处活内容均应进入 FTS 索引。
            self.assertTrue(
                db.execute(text("SELECT rowid FROM search_index WHERE search_index MATCH :q"), {"q": "Hello"}).all()
            )
            self.assertTrue(
                db.execute(text("SELECT rowid FROM search_index WHERE search_index MATCH :q"), {"q": "UniqueDocToken"}).all()
            )
            self.assertTrue(
                db.execute(text("SELECT rowid FROM search_index WHERE search_index MATCH :q"), {"q": "UniqueStoryMemoryToken"}).all()
            )

            # 章节内容更新后，旧 token 应消失、新 token 应出现（增量同步）。
            chapter = db.get(Chapter, "c1")
            self.assertIsNotNone(chapter)
            assert chapter is not None
            chapter.content_md = "Foobar baz"
            db.commit()

            result2 = search_index_service.rebuild_project_search_index(db=db, project_id="p1")
            db.commit()
            self.assertTrue(result2.get("ok"))

            old_rows = db.execute(text("SELECT rowid FROM search_index WHERE search_index MATCH :q"), {"q": "Hello"}).all()
            self.assertFalse(old_rows)

            new_rows = db.execute(text("SELECT rowid FROM search_index WHERE search_index MATCH :q"), {"q": "Foobar"}).all()
            self.assertTrue(new_rows)
