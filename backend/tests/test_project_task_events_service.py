from __future__ import annotations

import json
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
from app.services.characters_auto_update_service import schedule_characters_auto_update_task
from app.services.project_task_event_service import append_project_task_event, project_task_event_to_dict
from app.services.vector_rag_service import schedule_vector_rebuild_task


class _RecordingQueue:
    def __init__(self, *, raise_on_enqueue: bool = False) -> None:
        self.raise_on_enqueue = raise_on_enqueue
        self.calls: list[tuple[str, str]] = []

    def enqueue(self, *, kind: str, task_id: str) -> str:
        self.calls.append((kind, task_id))
        if self.raise_on_enqueue:
            raise RuntimeError("enqueue_failed")
        return task_id

    def enqueue_batch_generation_task(self, task_id: str) -> str:  # pragma: no cover - protocol compatibility
        return self.enqueue(kind="batch_generation", task_id=task_id)


class TestProjectTaskEventsService(unittest.TestCase):
    def setUp(self) -> None:
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
        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        with self.SessionLocal() as db:
            db.add(User(id="u1", display_name="u1"))
            db.add(Project(id="p1", owner_user_id="u1", name="Project 1", genre=None, logline=None))
            db.add(
                ProjectSettings(
                    project_id="p1",
                    auto_update_vector_enabled=True,
                    vector_index_dirty=True,
                )
            )
            db.commit()

    def test_append_project_task_event_persists_monotonic_seq(self) -> None:
        with self.SessionLocal() as db:
            task = ProjectTask(
                id="pt1",
                project_id="p1",
                actor_user_id="u1",
                kind="noop",
                status="queued",
                idempotency_key="noop:1",
                params_json=None,
                result_json=None,
                error_json=None,
                attempt=2,
            )
            db.add(task)
            db.commit()

            event1 = append_project_task_event(db, task=task, event_type="queued", source="test", payload={"reason": "seed"})
            event2 = append_project_task_event(db, task=task, event_type="running", source="test", payload={"reason": "seed2"})
            db.commit()

            self.assertLess(event1.seq, event2.seq)
            payload = project_task_event_to_dict(event2)
            self.assertEqual(payload["event_type"], "running")
            self.assertEqual(payload["payload"]["task"]["id"], "pt1")
            self.assertEqual(payload["payload"]["task"]["attempt"], 2)

    def test_vector_scheduler_new_task_emits_queued_event(self) -> None:
        q = _RecordingQueue()
        with self.SessionLocal() as db, patch("app.services.task_queue.get_task_queue", return_value=q):
            task_id = schedule_vector_rebuild_task(
                db=db,
                project_id="p1",
                actor_user_id="u1",
                request_id="rid-vector",
                reason="chapter_done",
            )

            self.assertTrue(str(task_id or "").strip())
            self.assertEqual(len(q.calls), 1)
            events = db.execute(select(ProjectTaskEvent).order_by(ProjectTaskEvent.seq.asc())).scalars().all()
            self.assertEqual([event.event_type for event in events], ["queued"])
            payload = project_task_event_to_dict(events[0])
            self.assertEqual(payload["payload"]["reason"], "chapter_done")

    def test_enqueue_failure_records_failed_event(self) -> None:
        q = _RecordingQueue(raise_on_enqueue=True)
        with self.SessionLocal() as db, patch("app.services.task_queue.get_task_queue", return_value=q):
            task_id = schedule_characters_auto_update_task(
                db=db,
                project_id="p1",
                actor_user_id="u1",
                request_id="rid-characters",
                chapter_id="chapter-1",
                chapter_token="T2",
                reason="chapter_done",
            )

            self.assertTrue(str(task_id or "").strip())
            row = db.get(ProjectTask, str(task_id))
            assert row is not None
            self.assertEqual(row.status, "failed")
            events = db.execute(select(ProjectTaskEvent).order_by(ProjectTaskEvent.seq.asc())).scalars().all()
            self.assertEqual([event.event_type for event in events], ["queued", "failed"])

