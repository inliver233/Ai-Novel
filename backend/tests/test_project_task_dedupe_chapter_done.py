from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.project import Project
from app.models.project_settings import ProjectSettings
from app.models.project_task import ProjectTask
from app.models.project_task_event import ProjectTaskEvent
from app.models.user import User
from app.services.project_task_service import schedule_chapter_done_tasks


class _NoopQueue:
    def enqueue(self, *, kind: str, task_id: str) -> str:  # type: ignore[no-untyped-def]
        return task_id

    def enqueue_batch_generation_task(self, task_id: str) -> str:
        return task_id


class TestProjectTaskDedupeChapterDone(unittest.TestCase):
    def test_dedupes_queued_tasks_keeps_latest_token(self) -> None:
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
                ProjectSettings.__table__,
                ProjectTask.__table__,
                ProjectTaskEvent.__table__,
            ],
        )
        SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        with SessionLocal() as db:
            db.add(User(id="u1", display_name="u1"))
            db.add(Project(id="p1", owner_user_id="u1", name="p1", genre=None, logline=None))
            db.add(
                ProjectSettings(
                    project_id="p1",
                    auto_update_characters_enabled=True,
                    auto_update_vector_enabled=False,
                    auto_update_search_enabled=False,
                )
            )
            db.commit()

            with patch("app.services.task_queue.get_task_queue", return_value=_NoopQueue()):
                schedule_chapter_done_tasks(
                    db=db,
                    project_id="p1",
                    actor_user_id="u1",
                    request_id="rid-1",
                    chapter_id="c1",
                    chapter_token="token-1",
                    reason="chapter_done",
                )
                schedule_chapter_done_tasks(
                    db=db,
                    project_id="p1",
                    actor_user_id="u1",
                    request_id="rid-2",
                    chapter_id="c1",
                    chapter_token="token-2",
                    reason="chapter_done",
                )

            rows = (
                db.execute(
                    select(ProjectTask).where(
                        ProjectTask.project_id == "p1",
                        ProjectTask.kind == "characters_auto_update",
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(len(rows), 2)
            by_status = {str(r.status): r for r in rows}
            self.assertIn("queued", by_status)
            self.assertIn("canceled", by_status)

            canceled = by_status["canceled"]
            queued = by_status["queued"]
            self.assertIn("token-1", str(canceled.idempotency_key))
            self.assertIn("token-2", str(queued.idempotency_key))
            self.assertIsNotNone(canceled.finished_at)

