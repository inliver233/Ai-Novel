from __future__ import annotations

import json
import unittest
from typing import Generator
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api.routes import batch_generation as batch_generation_routes
from app.core.errors import AppError
from app.db.base import Base
from app.db.session import get_db
from app.main import app_error_handler, validation_error_handler
from app.models.batch_generation_task import BatchGenerationQuotaGuard, BatchGenerationTask, BatchGenerationTaskItem
from app.models.chapter import Chapter
from app.models.generation_run import GenerationRun
from app.models.llm_profile import LLMProfile
from app.models.llm_preset import LLMPreset
from app.models.llm_task_preset import LLMTaskPreset
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_task import ProjectTask
from app.models.project_task_event import ProjectTaskEvent
from app.models.user import User
from app.services import batch_generation_service


def _make_test_app(SessionLocal: sessionmaker) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _test_user_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = "rid-batch-runtime"
        user_id = request.headers.get("X-Test-User")
        request.state.user_id = user_id
        request.state.authenticated_user_id = user_id
        request.state.session_expire_at = None
        request.state.auth_source = "test"
        return await call_next(request)

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.include_router(batch_generation_routes.router, prefix="/api")

    def _override_get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    return app


class TestBatchGenerationRuntimeLinkage(unittest.TestCase):
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
                LLMProfile.__table__,
                LLMPreset.__table__,
                LLMTaskPreset.__table__,
                GenerationRun.__table__,
                Project.__table__,
                Outline.__table__,
                Chapter.__table__,
                ProjectTask.__table__,
                ProjectTaskEvent.__table__,
                BatchGenerationTask.__table__,
                BatchGenerationQuotaGuard.__table__,
            ],
        )
        BatchGenerationTaskItem.__table__.create(bind=engine)

        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        self.app = _make_test_app(self.SessionLocal)

        with self.SessionLocal() as db:
            db.add(User(id="u_owner", display_name="owner"))
            project = Project(
                id="p1",
                owner_user_id="u_owner",
                active_outline_id="o1",
                llm_profile_id="llm-profile-1",
                name="Project 1",
                genre=None,
                logline=None,
            )
            db.add(project)
            db.add(
                LLMProfile(
                    id="llm-profile-1",
                    owner_user_id="u_owner",
                    name="Default",
                    provider="openai",
                    model="gpt-4o-mini",
                    api_key_ciphertext="test-ciphertext",
                    api_key_masked="***",
                )
            )
            db.add(Outline(id="o1", project_id="p1", title="Outline", content_md="", structure_json=None))
            db.add(LLMPreset(project_id="p1", provider="openai", model="gpt-4o-mini"))
            db.add(Chapter(id="c1", project_id="p1", outline_id="o1", number=1, title="第一章", plan="", content_md=None, summary=None))
            db.commit()

    def test_create_batch_task_creates_linked_project_task(self) -> None:
        client = TestClient(self.app)

        class _NoopQueue:
            def enqueue_batch_generation_task(self, task_id: str) -> str:
                return task_id

        with patch("app.api.routes.batch_generation.get_task_queue", return_value=_NoopQueue()):
            resp = client.post(
                "/api/projects/p1/batch_generation_tasks",
                headers={"X-Test-User": "u_owner"},
                json={"count": 1, "include_existing": True},
            )

        self.assertEqual(resp.status_code, 200)
        payload = resp.json().get("data") or {}
        task = payload.get("task") or {}
        self.assertTrue(str(task.get("project_task_id") or "").strip())
        self.assertEqual(task.get("failed_count"), 0)
        self.assertEqual(task.get("skipped_count"), 0)
        self.assertFalse(bool(task.get("pause_requested")))

        with self.SessionLocal() as db:
            batch_task = db.get(BatchGenerationTask, task.get("id"))
            self.assertIsNotNone(batch_task)
            assert batch_task is not None
            self.assertTrue(str(batch_task.project_task_id or "").strip())
            self.assertTrue(str(batch_task.checkpoint_json or "").strip())
            self.assertEqual(batch_task.runtime_provider, "openai")

            runtime_task = db.get(ProjectTask, batch_task.project_task_id)
            self.assertIsNotNone(runtime_task)
            assert runtime_task is not None
            self.assertEqual(runtime_task.kind, "batch_generation_orchestrator")
            self.assertEqual(runtime_task.status, "queued")
            params = json.loads(runtime_task.params_json or "{}")
            self.assertEqual(params.get("batch_task_id"), batch_task.id)

            events = db.execute(
                select(ProjectTaskEvent)
                .where(ProjectTaskEvent.task_id == runtime_task.id)
                .order_by(ProjectTaskEvent.seq.asc())
            ).scalars().all()
            self.assertEqual([event.event_type for event in events], ["queued"])

    def test_cancel_queued_batch_updates_linked_project_task(self) -> None:
        client = TestClient(self.app)

        class _NoopQueue:
            def enqueue_batch_generation_task(self, task_id: str) -> str:
                return task_id

        with patch("app.api.routes.batch_generation.get_task_queue", return_value=_NoopQueue()):
            created = client.post(
                "/api/projects/p1/batch_generation_tasks",
                headers={"X-Test-User": "u_owner"},
                json={"count": 1, "include_existing": True},
            )

        batch_task_id = ((created.json().get("data") or {}).get("task") or {}).get("id")
        self.assertTrue(str(batch_task_id or "").strip())

        resp = client.post(f"/api/batch_generation_tasks/{batch_task_id}/cancel", headers={"X-Test-User": "u_owner"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.json().get("data") or {}
        self.assertTrue(bool(payload.get("canceled")))
        self.assertEqual((payload.get("task") or {}).get("status"), "canceled")

        with self.SessionLocal() as db:
            batch_task = db.get(BatchGenerationTask, batch_task_id)
            self.assertIsNotNone(batch_task)
            assert batch_task is not None
            runtime_task = db.get(ProjectTask, batch_task.project_task_id)
            self.assertIsNotNone(runtime_task)
            assert runtime_task is not None
            self.assertEqual(runtime_task.status, "canceled")

            event_types = db.execute(
                select(ProjectTaskEvent.event_type)
                .where(ProjectTaskEvent.task_id == runtime_task.id)
                .order_by(ProjectTaskEvent.seq.asc())
            ).scalars().all()
            self.assertEqual(event_types, ["queued", "canceled"])

    def test_pause_and_resume_queued_batch_updates_linked_runtime_task(self) -> None:
        client = TestClient(self.app)

        class _NoopQueue:
            def enqueue_batch_generation_task(self, task_id: str) -> str:
                return task_id

        with patch("app.api.routes.batch_generation.get_task_queue", return_value=_NoopQueue()):
            created = client.post(
                "/api/projects/p1/batch_generation_tasks",
                headers={"X-Test-User": "u_owner"},
                json={"count": 1, "include_existing": True},
            )

            batch_task_id = ((created.json().get("data") or {}).get("task") or {}).get("id")
            paused = client.post(f"/api/batch_generation_tasks/{batch_task_id}/pause", headers={"X-Test-User": "u_owner"})
            resumed = client.post(f"/api/batch_generation_tasks/{batch_task_id}/resume", headers={"X-Test-User": "u_owner"})

        self.assertEqual(paused.status_code, 200)
        self.assertEqual(resumed.status_code, 200)
        self.assertTrue(bool((paused.json().get("data") or {}).get("paused")))
        self.assertTrue(bool((resumed.json().get("data") or {}).get("resumed")))

        with self.SessionLocal() as db:
            batch_task = db.get(BatchGenerationTask, batch_task_id)
            self.assertIsNotNone(batch_task)
            assert batch_task is not None
            self.assertEqual(batch_task.status, "queued")
            self.assertFalse(bool(batch_task.pause_requested))

            runtime_task = db.get(ProjectTask, batch_task.project_task_id)
            self.assertIsNotNone(runtime_task)
            assert runtime_task is not None
            self.assertEqual(runtime_task.status, "queued")

            event_types = db.execute(
                select(ProjectTaskEvent.event_type)
                .where(ProjectTaskEvent.task_id == runtime_task.id)
                .order_by(ProjectTaskEvent.seq.asc())
            ).scalars().all()
            self.assertEqual(event_types, ["queued", "paused", "resumed"])

    def test_resume_paused_batch_with_failed_items_requires_retry_or_skip(self) -> None:
        client = TestClient(self.app)

        class _NoopQueue:
            def enqueue_batch_generation_task(self, task_id: str) -> str:
                return task_id

        with patch("app.api.routes.batch_generation.get_task_queue", return_value=_NoopQueue()):
            created = client.post(
                "/api/projects/p1/batch_generation_tasks",
                headers={"X-Test-User": "u_owner"},
                json={"count": 1, "include_existing": True},
            )

        batch_task_id = ((created.json().get("data") or {}).get("task") or {}).get("id")
        self.assertTrue(str(batch_task_id or "").strip())

        with self.SessionLocal() as db:
            batch_task = db.get(BatchGenerationTask, batch_task_id)
            self.assertIsNotNone(batch_task)
            assert batch_task is not None
            item = db.execute(select(BatchGenerationTaskItem).where(BatchGenerationTaskItem.task_id == batch_task_id)).scalars().first()
            self.assertIsNotNone(item)
            assert item is not None
            item.status = "failed"
            item.error_message = "boom"
            batch_task.status = "paused"
            batch_task.pause_requested = True
            batch_task.failed_count = 1
            runtime_task = db.get(ProjectTask, batch_task.project_task_id)
            self.assertIsNotNone(runtime_task)
            assert runtime_task is not None
            runtime_task.status = "paused"
            db.commit()

        resp = client.post(f"/api/batch_generation_tasks/{batch_task_id}/resume", headers={"X-Test-User": "u_owner"})
        self.assertEqual(resp.status_code, 409)
        payload = resp.json()
        self.assertEqual(((payload.get("error") or {}).get("details") or {}).get("failed_chapter_numbers"), [1])

    def test_retry_failed_batch_requeues_failed_items(self) -> None:
        client = TestClient(self.app)

        class _NoopQueue:
            def enqueue_batch_generation_task(self, task_id: str) -> str:
                return task_id

        with patch("app.api.routes.batch_generation.get_task_queue", return_value=_NoopQueue()):
            created = client.post(
                "/api/projects/p1/batch_generation_tasks",
                headers={"X-Test-User": "u_owner"},
                json={"count": 1, "include_existing": True},
            )

            batch_task_id = ((created.json().get("data") or {}).get("task") or {}).get("id")

            with self.SessionLocal() as db:
                batch_task = db.get(BatchGenerationTask, batch_task_id)
                self.assertIsNotNone(batch_task)
                assert batch_task is not None
                item = db.execute(select(BatchGenerationTaskItem).where(BatchGenerationTaskItem.task_id == batch_task_id)).scalars().first()
                self.assertIsNotNone(item)
                assert item is not None
                item.status = "failed"
                item.error_message = "boom"
                item.last_error_json = json.dumps({"code": "X", "message": "boom"}, ensure_ascii=False)
                batch_task.status = "paused"
                batch_task.pause_requested = True
                batch_task.failed_count = 1
                runtime_task = db.get(ProjectTask, batch_task.project_task_id)
                self.assertIsNotNone(runtime_task)
                assert runtime_task is not None
                runtime_task.status = "paused"
                db.commit()

            retried = client.post(f"/api/batch_generation_tasks/{batch_task_id}/retry_failed", headers={"X-Test-User": "u_owner"})

        self.assertEqual(retried.status_code, 200)
        self.assertTrue(bool((retried.json().get("data") or {}).get("retried")))

        with self.SessionLocal() as db:
            batch_task = db.get(BatchGenerationTask, batch_task_id)
            self.assertIsNotNone(batch_task)
            assert batch_task is not None
            item = db.execute(select(BatchGenerationTaskItem).where(BatchGenerationTaskItem.task_id == batch_task_id)).scalars().first()
            self.assertIsNotNone(item)
            assert item is not None
            self.assertEqual(batch_task.status, "queued")
            self.assertEqual(batch_task.failed_count, 0)
            self.assertFalse(bool(batch_task.pause_requested))
            self.assertEqual(item.status, "queued")
            self.assertIsNone(item.error_message)
            self.assertIsNone(item.last_error_json)

            runtime_task = db.get(ProjectTask, batch_task.project_task_id)
            self.assertIsNotNone(runtime_task)
            assert runtime_task is not None
            self.assertEqual(runtime_task.status, "queued")

            event_types = db.execute(
                select(ProjectTaskEvent.event_type)
                .where(ProjectTaskEvent.task_id == runtime_task.id)
                .order_by(ProjectTaskEvent.seq.asc())
            ).scalars().all()
            self.assertEqual(event_types, ["queued", "step_requeued", "retry"])

    def test_skip_failed_batch_can_finish_without_pending_items(self) -> None:
        client = TestClient(self.app)

        class _NoopQueue:
            def enqueue_batch_generation_task(self, task_id: str) -> str:
                return task_id

        with patch("app.api.routes.batch_generation.get_task_queue", return_value=_NoopQueue()):
            created = client.post(
                "/api/projects/p1/batch_generation_tasks",
                headers={"X-Test-User": "u_owner"},
                json={"count": 1, "include_existing": True},
            )

        batch_task_id = ((created.json().get("data") or {}).get("task") or {}).get("id")
        with self.SessionLocal() as db:
            batch_task = db.get(BatchGenerationTask, batch_task_id)
            self.assertIsNotNone(batch_task)
            assert batch_task is not None
            item = db.execute(select(BatchGenerationTaskItem).where(BatchGenerationTaskItem.task_id == batch_task_id)).scalars().first()
            self.assertIsNotNone(item)
            assert item is not None
            item.status = "failed"
            item.error_message = "boom"
            batch_task.status = "paused"
            batch_task.pause_requested = True
            batch_task.failed_count = 1
            runtime_task = db.get(ProjectTask, batch_task.project_task_id)
            self.assertIsNotNone(runtime_task)
            assert runtime_task is not None
            runtime_task.status = "paused"
            db.commit()

        skipped = client.post(f"/api/batch_generation_tasks/{batch_task_id}/skip_failed", headers={"X-Test-User": "u_owner"})
        self.assertEqual(skipped.status_code, 200)
        self.assertTrue(bool((skipped.json().get("data") or {}).get("skipped")))

        with self.SessionLocal() as db:
            batch_task = db.get(BatchGenerationTask, batch_task_id)
            self.assertIsNotNone(batch_task)
            assert batch_task is not None
            item = db.execute(select(BatchGenerationTaskItem).where(BatchGenerationTaskItem.task_id == batch_task_id)).scalars().first()
            self.assertIsNotNone(item)
            assert item is not None
            self.assertEqual(batch_task.status, "succeeded")
            self.assertEqual(batch_task.failed_count, 0)
            self.assertEqual(batch_task.skipped_count, 1)
            self.assertEqual(item.status, "skipped")

            runtime_task = db.get(ProjectTask, batch_task.project_task_id)
            self.assertIsNotNone(runtime_task)
            assert runtime_task is not None
            self.assertEqual(runtime_task.status, "succeeded")

    def test_create_batch_task_accepts_count_over_20_with_new_limit(self) -> None:
        client = TestClient(self.app)

        with self.SessionLocal() as db:
            for number in range(2, 23):
                db.add(Chapter(id=f"c{number}", project_id="p1", outline_id="o1", number=number, title=f"第{number}章", plan="", content_md=None, summary=None))
            db.commit()

        class _NoopQueue:
            def enqueue_batch_generation_task(self, task_id: str) -> str:
                return task_id

        with patch("app.api.routes.batch_generation.get_task_queue", return_value=_NoopQueue()):
            resp = client.post(
                "/api/projects/p1/batch_generation_tasks",
                headers={"X-Test-User": "u_owner"},
                json={"count": 21, "include_existing": True},
            )

        self.assertEqual(resp.status_code, 200)
        task = ((resp.json().get("data") or {}).get("task") or {})
        self.assertEqual(task.get("total_count"), 21)

    def test_create_uses_resolved_preset_provider_instead_of_request_header(self) -> None:
        client = TestClient(self.app)
        with self.SessionLocal() as db:
            db.add(
                LLMTaskPreset(
                    project_id="p1",
                    task_key="chapter_generate",
                    provider="google",
                    model="gemini-2.0-flash",
                )
            )
            db.commit()

        class _NoopQueue:
            def enqueue_batch_generation_task(self, task_id: str) -> str:
                return task_id

        with patch("app.api.routes.batch_generation.get_task_queue", return_value=_NoopQueue()):
            resp = client.post(
                "/api/projects/p1/batch_generation_tasks",
                headers={"X-Test-User": "u_owner", "X-LLM-Provider": "anthropic"},
                json={"count": 1, "include_existing": True},
            )

        self.assertEqual(resp.status_code, 200)
        task = (resp.json().get("data") or {}).get("task") or {}
        self.assertEqual(task.get("runtime_provider"), "gemini")
        with self.SessionLocal() as db:
            stored = db.get(BatchGenerationTask, task.get("id"))
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored.runtime_provider, "gemini")
            self.assertNotIn("runtime_provider", json.loads(stored.params_json or "{}"))

    def test_create_rejects_oversized_provider_header(self) -> None:
        client = TestClient(self.app)
        resp = client.post(
            "/api/projects/p1/batch_generation_tasks",
            headers={"X-Test-User": "u_owner", "X-LLM-Provider": "x" * 65},
            json={"count": 1, "include_existing": True},
        )
        self.assertEqual(resp.status_code, 400)

    def test_project_quota_respects_configured_limit_greater_than_one(self) -> None:
        client = TestClient(self.app)

        class _NoopQueue:
            def enqueue_batch_generation_task(self, task_id: str) -> str:
                return task_id

        with (
            patch("app.api.routes.batch_generation.get_task_queue", return_value=_NoopQueue()),
            patch.object(batch_generation_routes.settings, "batch_generation_project_active_limit", 2),
        ):
            first = client.post(
                "/api/projects/p1/batch_generation_tasks",
                headers={"X-Test-User": "u_owner"},
                json={"count": 1, "include_existing": True},
            )
            second = client.post(
                "/api/projects/p1/batch_generation_tasks",
                headers={"X-Test-User": "u_owner"},
                json={"count": 1, "include_existing": True},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

    def test_worker_fails_closed_when_preset_provider_changes_after_create(self) -> None:
        client = TestClient(self.app)

        class _NoopQueue:
            def enqueue_batch_generation_task(self, task_id: str) -> str:
                return task_id

        with patch("app.api.routes.batch_generation.get_task_queue", return_value=_NoopQueue()):
            created = client.post(
                "/api/projects/p1/batch_generation_tasks",
                headers={"X-Test-User": "u_owner"},
                json={"count": 1, "include_existing": True},
            )
        task_id = ((created.json().get("data") or {}).get("task") or {}).get("id")
        self.assertTrue(str(task_id or "").strip())

        with self.SessionLocal() as db:
            preset = db.get(LLMPreset, "p1")
            self.assertIsNotNone(preset)
            assert preset is not None
            preset.provider = "anthropic"
            preset.model = "claude-3-5-sonnet-latest"
            db.commit()

        with (
            patch.object(batch_generation_service, "SessionLocal", self.SessionLocal),
            patch("app.services.llm_task_preset_resolver.resolve_api_key_for_profile", return_value="sk-test"),
            patch.object(batch_generation_service, "run_chapter_generate_llm_step") as llm_step,
        ):
            batch_generation_service.run_batch_generation_task(task_id=str(task_id))

        llm_step.assert_not_called()
        with self.SessionLocal() as db:
            task = db.get(BatchGenerationTask, task_id)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.runtime_provider, "openai")
            self.assertEqual(task.status, "paused")
            self.assertTrue(bool(task.pause_requested))
            error = json.loads(task.error_json or "{}")
            self.assertEqual(error.get("code"), "BATCH_GENERATION_PROVIDER_DRIFT")
