from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.project import Project
from app.models.project_settings import ProjectSettings
from app.models.project_task import ProjectTask
from app.models.project_task_event import ProjectTaskEvent
from app.models.user import User
from app.services import project_task_service, vector_build, vector_rag_service
from app.services.vector_index_state import mark_vector_index_dirty


class _RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def enqueue(self, *, kind: str, task_id: str) -> str:
        self.calls.append((kind, task_id))
        return task_id


def _factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'vector-revision-fence.db'}",
        connect_args={"check_same_thread": False},
    )
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
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add(User(id="u1", display_name="User 1"))
        db.add(Project(id="p1", owner_user_id="u1", name="Project 1", genre=None, logline=None))
        db.add(
            ProjectSettings(
                project_id="p1",
                vector_index_dirty=True,
                vector_dirty_revision=1,
            )
        )
        db.commit()
    return engine, factory


def test_vector_worker_keeps_newer_mutation_dirty_and_runs_followup(tmp_path) -> None:
    engine, factory = _factory(tmp_path)
    queue = _RecordingQueue()
    rebuild_calls = 0

    def _rebuild_with_one_concurrent_mutation(**_kwargs):
        nonlocal rebuild_calls
        rebuild_calls += 1
        if rebuild_calls == 1:
            with factory() as writer:
                revision = mark_vector_index_dirty(writer, project_id="p1")
                writer.commit()
                assert revision == 2
        return {"enabled": True, "skipped": False, "rebuilt": 1, "backend": "test"}

    try:
        with (
            patch.object(project_task_service, "SessionLocal", factory),
            patch.object(vector_build, "SessionLocal", factory),
            patch.object(project_task_service, "start_project_task_heartbeat", return_value=None),
            patch("app.services.task_queue.get_task_queue", return_value=queue),
            patch("app.services.vector_kb_service.list_kbs", return_value=[]),
            patch.object(vector_rag_service, "vector_rag_status", return_value={"enabled": True}),
            patch.object(vector_rag_service, "build_project_chunks", return_value=[SimpleNamespace(id="chunk")]),
            patch.object(vector_rag_service, "rebuild_project", side_effect=_rebuild_with_one_concurrent_mutation),
        ):
            with factory() as db:
                first_task_id = vector_build.schedule_vector_rebuild_task(
                    db=db,
                    project_id="p1",
                    actor_user_id="u1",
                    request_id="request-1",
                    reason="chapter_updated",
                )

            assert first_task_id is not None
            project_task_service.run_project_task(task_id=first_task_id)

            with factory() as db:
                first_task = db.get(ProjectTask, first_task_id)
                settings = db.get(ProjectSettings, "p1")
                assert first_task is not None
                assert settings is not None
                assert first_task.status == "queued", first_task.error_json
                assert first_task.result_json is None
                assert json.loads(first_task.params_json or "{}")["dirty_revision"] == 2
                assert settings.vector_index_dirty is True
                assert settings.vector_dirty_revision == 2
                assert settings.last_vector_build_at is None
                events = (
                    db.execute(
                        select(ProjectTaskEvent)
                        .where(ProjectTaskEvent.task_id == first_task_id)
                        .order_by(ProjectTaskEvent.seq)
                    )
                    .scalars()
                    .all()
                )
                assert [event.event_type for event in events] == ["queued", "running", "queued"]
                stale_event = json.loads(events[-1].payload_json or "{}")
                assert stale_event["result"]["build_revision"] == 1
                assert stale_event["result"]["current_dirty_revision"] == 2

            project_task_service.run_project_task(task_id=first_task_id)

            with factory() as db:
                followup_task = db.get(ProjectTask, first_task_id)
                settings = db.get(ProjectSettings, "p1")
                assert followup_task is not None
                assert settings is not None
                followup_result = json.loads(followup_task.result_json or "{}")
                assert followup_task.status == "succeeded"
                assert followup_result["build_revision"] == 2
                assert followup_result["stale"] is False
                assert followup_task.attempt == 2
                assert settings.vector_index_dirty is False
                assert settings.vector_dirty_revision == 2
                assert settings.last_vector_build_at is not None

            tasks = []
            with factory() as db:
                tasks = (
                    db.execute(
                        select(ProjectTask).where(ProjectTask.project_id == "p1").order_by(ProjectTask.created_at)
                    )
                    .scalars()
                    .all()
                )
            assert len(tasks) == 1
            assert rebuild_calls == 2
            assert queue.calls == [("project_task", first_task_id), ("project_task", first_task_id)]
    finally:
        engine.dispose()


def test_vector_worker_finalization_commit_failure_preserves_dirty_and_retries(tmp_path) -> None:
    engine, factory = _factory(tmp_path)
    queue = _RecordingQueue()
    rebuild_calls = 0

    def _successful_external_rebuild(**_kwargs):
        nonlocal rebuild_calls
        rebuild_calls += 1
        return {"enabled": True, "skipped": False, "rebuilt": 1, "backend": "test"}

    with factory() as db:
        db.add(
            ProjectTask(
                id="vector-task-commit-fail",
                project_id="p1",
                actor_user_id="u1",
                kind="vector_rebuild",
                status="queued",
                idempotency_key="vector:project:since:none:v1",
                params_json=json.dumps({"request_id": "request-commit-fail", "dirty_revision": 1}),
                result_json=None,
                error_json=None,
            )
        )
        db.commit()

    session_count = 0

    def _sessions_with_finalization_failure():
        nonlocal session_count
        session_count += 1
        db = factory()
        if session_count == 1:
            real_commit = db.commit
            commit_count = 0

            def _commit_with_finalization_failure():
                nonlocal commit_count
                commit_count += 1
                if commit_count == 3:
                    raise RuntimeError("vector finalization commit failed")
                return real_commit()

            db.commit = _commit_with_finalization_failure
        return db

    try:
        with (
            patch.object(project_task_service, "SessionLocal", _sessions_with_finalization_failure),
            patch.object(project_task_service, "start_project_task_heartbeat", return_value=None),
            patch("app.services.vector_kb_service.list_kbs", return_value=[]),
            patch.object(vector_rag_service, "vector_rag_status", return_value={"enabled": True}),
            patch.object(vector_rag_service, "build_project_chunks", return_value=[SimpleNamespace(id="chunk")]),
            patch.object(vector_rag_service, "rebuild_project", side_effect=_successful_external_rebuild),
        ):
            project_task_service.run_project_task(task_id="vector-task-commit-fail")

        with factory() as db:
            task = db.get(ProjectTask, "vector-task-commit-fail")
            settings = db.get(ProjectSettings, "p1")
            assert task is not None
            assert settings is not None
            assert task.status == "failed"
            assert "vector finalization commit failed" in (task.error_json or "")
            assert settings.vector_index_dirty is True
            assert settings.vector_dirty_revision == 1
            assert settings.last_vector_build_at is None
            events = (
                db.execute(
                    select(ProjectTaskEvent)
                    .where(ProjectTaskEvent.task_id == "vector-task-commit-fail")
                    .order_by(ProjectTaskEvent.seq)
                )
                .scalars()
                .all()
            )
            assert [event.event_type for event in events] == ["running", "failed"]
        assert rebuild_calls == 1

        with (
            factory() as db,
            patch("app.services.task_queue.get_task_queue", return_value=queue),
        ):
            task = db.get(ProjectTask, "vector-task-commit-fail")
            assert task is not None
            project_task_service.retry_project_task(db=db, task=task)

        with (
            patch.object(project_task_service, "SessionLocal", factory),
            patch.object(project_task_service, "start_project_task_heartbeat", return_value=None),
            patch("app.services.vector_kb_service.list_kbs", return_value=[]),
            patch.object(vector_rag_service, "vector_rag_status", return_value={"enabled": True}),
            patch.object(vector_rag_service, "build_project_chunks", return_value=[SimpleNamespace(id="chunk")]),
            patch.object(vector_rag_service, "rebuild_project", side_effect=_successful_external_rebuild),
        ):
            project_task_service.run_project_task(task_id="vector-task-commit-fail")

        with factory() as db:
            task = db.get(ProjectTask, "vector-task-commit-fail")
            settings = db.get(ProjectSettings, "p1")
            assert task is not None
            assert settings is not None
            assert task.status == "succeeded"
            assert task.attempt == 2
            assert settings.vector_index_dirty is False
            assert settings.vector_dirty_revision == 1
            assert settings.last_vector_build_at is not None
        assert rebuild_calls == 2
        assert queue.calls == [("project_task", "vector-task-commit-fail")]
    finally:
        engine.dispose()
