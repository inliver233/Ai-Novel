from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import select
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api.routes import chapters as chapters_routes
from app.core.errors import AppError
from app.db.base import Base
from app.main import app_error_handler, validation_error_handler
from app.models.chapter import Chapter
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_settings import ProjectSettings
from app.models.project_task import ProjectTask
from app.models.project_task_event import ProjectTaskEvent
from app.models.user import User


class _DummyQueue:
    def enqueue(self, *, kind: str, task_id: str) -> None:
        return None


def _make_test_app(SessionLocal: sessionmaker) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _test_user_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = "rid-test"
        user_id = request.headers.get("X-Test-User")
        request.state.user_id = user_id
        request.state.authenticated_user_id = user_id
        request.state.session_expire_at = None
        request.state.auth_source = "test"
        return await call_next(request)

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.include_router(chapters_routes.router, prefix="/api")

    return app


class TestChapterTriggerAutoUpdatesEndpoint(unittest.TestCase):
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
                Outline.__table__,
                Chapter.__table__,
                ProjectTask.__table__,
                ProjectTaskEvent.__table__,
            ],
        )

        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        self.app = _make_test_app(self.SessionLocal)

        with self.SessionLocal() as db:
            db.add(User(id="u_owner", display_name="owner"))
            db.add(Project(id="p1", owner_user_id="u_owner", name="Project 1", genre=None, logline=None))
            db.add(
                ProjectSettings(
                    project_id="p1",
                    vector_index_dirty=True,
                )
            )
            db.add(Outline(id="o1", project_id="p1", title="Outline 1", content_md=None, structure_json=None))
            db.add(
                Chapter(
                    id="c1",
                    project_id="p1",
                    outline_id="o1",
                    number=1,
                    title="Chapter 1",
                    plan="hello",
                    content_md="content",
                    summary="summary",
                    status="draft",
                )
            )
            db.commit()

    def test_trigger_chapter_auto_updates_is_idempotent_by_generation_run_id(self) -> None:
        client = TestClient(self.app)
        with patch("app.db.session.SessionLocal", self.SessionLocal), patch(
            "app.services.task_queue.get_task_queue", return_value=_DummyQueue()
        ), patch(
            "app.services.characters_auto_update_service.get_task_queue", return_value=_DummyQueue()
        ):
            resp1 = client.post(
                "/api/chapters/c1/trigger_auto_updates",
                headers={"X-Test-User": "u_owner"},
                json={"generation_run_id": "run-1"},
            )
            self.assertEqual(resp1.status_code, 200)
            payload1 = resp1.json()
            self.assertTrue(payload1.get("ok"))
            data1 = payload1.get("data") or {}
            tasks1 = data1.get("tasks") or {}
            # lite 分支删除了 worldbook/graph auto_update；当前活任务是
            # vector_rebuild / search_rebuild / characters_auto_update。至少
            # characters_auto_update 应被调度（默认开关开启）。
            self.assertTrue(str(tasks1.get("characters_auto_update") or ""))

            with self.SessionLocal() as db:
                before = db.execute(select(ProjectTask)).scalars().all()

            resp2 = client.post(
                "/api/chapters/c1/trigger_auto_updates",
                headers={"X-Test-User": "u_owner"},
                json={"generation_run_id": "run-1"},
            )
            self.assertEqual(resp2.status_code, 200)
            payload2 = resp2.json()
            self.assertTrue(payload2.get("ok"))
            data2 = payload2.get("data") or {}
            tasks2 = data2.get("tasks") or {}

            self.assertEqual(tasks2, tasks1)

            with self.SessionLocal() as db:
                after = db.execute(select(ProjectTask)).scalars().all()

            self.assertEqual(len(after), len(before))
