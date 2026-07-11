from __future__ import annotations

import os
import json
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from starlette.requests import Request
from alembic import command
from alembic.runtime.migration import MigrationContext
from sqlalchemy.orm import sessionmaker

from app.db.migrations import _alembic_config
from app.db.utils import new_id
from app.core.config import settings
from app.core.errors import AppError
from app.api.routes import batch_generation as batch_generation_routes
from app.api.routes import chapters as chapters_routes
from app.models.batch_generation_task import BatchGenerationQuotaGuard, BatchGenerationTask, BatchGenerationTaskItem
from app.models.chapter import Chapter
from app.models.llm_preset import LLMPreset
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_settings import ProjectSettings
from app.models.project_task import ProjectTask
from app.models.project_task_event import ProjectTaskEvent
from app.models.prompt_preset import PromptPreset
from app.models.user import User
from app.schemas.batch_generation import BatchGenerationCreateRequest
from app.schemas.chapters import BulkCreateRequest
from app.services.prompt_preset_defaults import sync_builtin_prompt_defaults
from app.services.batch_generation_quota import (
    enter_batch_generation_quota_admission,
    lock_and_enforce_batch_generation_quotas,
)
from app.services import batch_generation_commands
from app.services import batch_generation_application
from app.services import batch_generation_service
from app.services import vector_retrieval, vector_storage
from app.services.vector_types import VectorChunk
from scripts.check_alembic import (
    SchemaContractError,
    assert_schema_matches_metadata,
    check_empty_database,
    migration_head,
)
from scripts import migrate_sqlite_to_postgres as sqlite_pg_migrator


PRE_CLEANUP_REVISION = "9f3a7c2d1e4b"
HEAD_REVISION = "a4c9d2e7f1b3"
EXPECTED_DATABASE = "ainovel_schema_ci"
DESTRUCTIVE_SENTINEL = "I_UNDERSTAND_THIS_DROPS_PUBLIC_SCHEMA"
RETIRED_TABLES = {
    "entities",
    "relations",
    "events",
    "foreshadows",
    "evidence",
    "memory_change_sets",
    "memory_change_set_items",
    "memory_tasks",
    "project_tables",
    "project_table_rows",
    "worldbook_entries",
    "glossary_terms",
    "plot_analysis",
    "fractal_memory",
}


def _validate_destructive_url(database_url: str, *, sentinel: str | None) -> None:
    url = sa.engine.make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("TEST_POSTGRES_URL must be a PostgreSQL URL")
    if url.host not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("destructive PostgreSQL integration requires a loopback host")
    if url.database != EXPECTED_DATABASE:
        raise RuntimeError(f"destructive PostgreSQL integration requires database {EXPECTED_DATABASE!r}")
    if sentinel != DESTRUCTIVE_SENTINEL:
        raise RuntimeError("destructive PostgreSQL integration sentinel is missing or incorrect")


def _postgres_url() -> str:
    enabled = os.getenv("AINOVEL_POSTGRES_INTEGRATION") == "1"
    database_url = os.getenv("TEST_POSTGRES_URL", "")
    if not enabled or not database_url:
        pytest.skip("set AINOVEL_POSTGRES_INTEGRATION=1 and TEST_POSTGRES_URL to run")
    try:
        _validate_destructive_url(
            database_url,
            sentinel=os.getenv("AINOVEL_POSTGRES_INTEGRATION_SENTINEL"),
        )
    except RuntimeError as exc:
        pytest.fail(str(exc))
    return database_url


@pytest.mark.parametrize(
    ("database_url", "sentinel", "message"),
    [
        (
            "postgresql+psycopg2://ainovel:ainovel@db.example/ainovel_schema_ci",
            DESTRUCTIVE_SENTINEL,
            "loopback host",
        ),
        (
            "postgresql+psycopg2://ainovel:ainovel@localhost/production",
            DESTRUCTIVE_SENTINEL,
            "requires database",
        ),
        (
            "postgresql+psycopg2://ainovel:ainovel@localhost/ainovel_schema_ci",
            "wrong",
            "sentinel",
        ),
    ],
)
def test_destructive_postgres_identity_fails_closed(
    database_url: str,
    sentinel: str,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        _validate_destructive_url(database_url, sentinel=sentinel)


def _upgrade(database_url: str, revision: str) -> None:
    config = _alembic_config(database_url=database_url)
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(config, revision)
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def _downgrade(database_url: str, revision: str) -> None:
    config = _alembic_config(database_url=database_url)
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.downgrade(config, revision)
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def _alembic_check(database_url: str) -> None:
    config = _alembic_config(database_url=database_url)
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.check(config)
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def _assert_postgres_quota_race(
    session_factory: sessionmaker,
    *,
    dimension: str,
    first: tuple[str, str, str],
    second: tuple[str, str, str],
    limits: tuple[int, int, int],
) -> None:
    with session_factory() as db:
        db.query(BatchGenerationTask).delete(synchronize_session=False)
        db.query(BatchGenerationQuotaGuard).delete(synchronize_session=False)
        db.commit()

    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []

    def _admit(index: int, values: tuple[str, str, str]) -> None:
        project_id, user_id, provider = values
        try:
            with session_factory() as db:
                barrier.wait(timeout=10)
                enter_batch_generation_quota_admission(db)
                lock_and_enforce_batch_generation_quotas(
                    db,
                    project_id=project_id,
                    user_id=user_id,
                    provider=provider,
                )
                db.add(
                    BatchGenerationTask(
                        id=f"postgres-{dimension}-{index}",
                        project_id=project_id,
                        outline_id=f"outline-{project_id}",
                        actor_user_id=user_id,
                        runtime_provider=provider,
                        status="queued",
                    )
                )
                db.commit()
                results.append("admitted")
        except AppError as exc:
            results.append(str(exc.details.get("quota")))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    project_limit, user_limit, provider_limit = limits
    with (
        patch.object(settings, "batch_generation_project_active_limit", project_limit),
        patch.object(settings, "batch_generation_user_active_limit", user_limit),
        patch.object(settings, "batch_generation_provider_active_limit", provider_limit),
    ):
        threads = [
            threading.Thread(target=_admit, args=(1, first)),
            threading.Thread(target=_admit, args=(2, second)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == ["admitted", dimension]
    with session_factory() as db:
        assert db.query(BatchGenerationTask).count() == 1


def _assert_postgres_create_route_race(session_factory: sessionmaker) -> None:
    before_outline = threading.Barrier(2)
    after_chapter = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []
    original_ensure = batch_generation_application.ensure_active_outline

    def _ensure_with_candidate(db, *, project):  # type: ignore[no-untyped-def]
        before_outline.wait(timeout=10)
        outline = original_ensure(db, project=project)
        chapter_id = new_id()
        if db.get(Chapter, chapter_id) is None:
            db.add(
                Chapter(
                    id=chapter_id,
                    project_id=project.id,
                    outline_id=outline.id,
                    number=1,
                    title="Chapter",
                )
            )
            db.commit()
        after_chapter.wait(timeout=10)
        return outline

    class _NoopQueue:
        def enqueue_batch_generation_task(self, task_id: str) -> str:
            return task_id

    def _create(index: int) -> None:
        try:
            with session_factory() as db:
                request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
                request.state.request_id = f"postgres-create-{index}"
                batch_generation_routes.create_batch_generation_task(
                    request=request,
                    db=db,
                    user_id="quota-user-1",
                    project_id="quota-create-project",
                    body=BatchGenerationCreateRequest(count=1, include_existing=True),
                )
                results.append("admitted")
        except AppError as exc:
            results.append(str(exc.details.get("quota")))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    with (
        patch.object(batch_generation_application, "ensure_active_outline", side_effect=_ensure_with_candidate),
        patch("app.services.batch_generation_commands.get_task_queue", return_value=_NoopQueue()),
        patch.object(settings, "batch_generation_project_active_limit", 1),
        patch.object(settings, "batch_generation_user_active_limit", 10),
        patch.object(settings, "batch_generation_provider_active_limit", 10),
    ):
        threads = [threading.Thread(target=_create, args=(1,)), threading.Thread(target=_create, args=(2,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == ["admitted", "project"]
    with session_factory() as db:
        assert db.query(BatchGenerationTask).filter_by(project_id="quota-create-project").count() == 1


def _assert_postgres_batch_command_races(session_factory: sessionmaker) -> None:
    class _NoopQueue:
        def enqueue_batch_generation_task(self, task_id: str) -> str:
            return task_id

    for competing_action in ("resume", "retry_failed"):
        task_id = f"pg-{competing_action}"
        item_id = f"item-{competing_action}"
        with session_factory() as db:
            db.add(
                BatchGenerationTask(
                    id=task_id,
                    project_id="quota-project-1",
                    outline_id="outline-quota-project-1",
                    actor_user_id="quota-user-1",
                    runtime_provider="openai",
                    status="paused",
                    total_count=1,
                    failed_count=1 if competing_action == "retry_failed" else 0,
                    pause_requested=True,
                )
            )
            db.add(
                BatchGenerationTaskItem(
                    id=item_id,
                    task_id=task_id,
                    chapter_id=None,
                    chapter_number=1,
                    status="failed" if competing_action == "retry_failed" else "queued",
                )
            )
            db.commit()

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _run(action: str) -> None:
            try:
                with session_factory() as db:
                    barrier.wait(timeout=10)
                    if action == "cancel":
                        batch_generation_commands.cancel_batch_generation_task(
                            db,
                            task_id=task_id,
                            authorized_project_id="quota-project-1",
                        )
                    elif action == "resume":
                        batch_generation_commands.resume_batch_generation_task(
                            db,
                            task_id=task_id,
                            authorized_project_id="quota-project-1",
                            actor_user_id="quota-user-1",
                        )
                    else:
                        batch_generation_commands.retry_failed_batch_generation_task(
                            db,
                            task_id=task_id,
                            authorized_project_id="quota-project-1",
                            actor_user_id="quota-user-1",
                        )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with patch.object(batch_generation_commands, "get_task_queue", return_value=_NoopQueue()):
            threads = [
                threading.Thread(target=_run, args=("cancel",)),
                threading.Thread(target=_run, args=(competing_action,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        with session_factory() as db:
            task = db.get(BatchGenerationTask, task_id)
            item = db.get(BatchGenerationTaskItem, item_id)
            assert task is not None
            assert item is not None
            assert task.status == "canceled"
            assert task.cancel_requested is True
            assert item.status != "queued"
            db.delete(item)
            db.delete(task)
            db.commit()


def _assert_postgres_worker_control_races(session_factory: sessionmaker) -> None:
    for action in ("cancel", "pause"):
        task_id = f"pg-worker-{action}"
        item_id = f"item-worker-{action}"
        with session_factory() as db:
            db.add(
                BatchGenerationTask(
                    id=task_id,
                    project_id="quota-project-1",
                    outline_id="outline-quota-project-1",
                    actor_user_id="quota-user-1",
                    runtime_provider="openai",
                    status="queued",
                    total_count=1,
                    params_json='{"context": {}}',
                )
            )
            db.add(
                BatchGenerationTaskItem(id=item_id, task_id=task_id, chapter_id=None, chapter_number=1, status="queued")
            )
            db.commit()

        prepare_entered = threading.Event()
        release_prepare = threading.Event()
        errors: list[BaseException] = []

        def _blocked_prepare(**_kwargs):  # type: ignore[no-untyped-def]
            prepare_entered.set()
            if not release_prepare.wait(timeout=15):
                raise RuntimeError("postgres worker race timed out")
            return (
                SimpleNamespace(id="quota-project-1"),
                SimpleNamespace(provider="openai"),
                "key",
                "",
                "",
                "",
                "",
                "",
                {},
            )

        def _run_worker() -> None:
            try:
                batch_generation_service.run_batch_generation_task(task_id=task_id)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with (
            patch.object(batch_generation_service, "SessionLocal", session_factory),
            patch.object(batch_generation_service, "_prepare_project_context", side_effect=_blocked_prepare),
        ):
            worker = threading.Thread(target=_run_worker)
            worker.start()
            assert prepare_entered.wait(timeout=15)
            with session_factory() as db:
                command = (
                    batch_generation_commands.cancel_batch_generation_task
                    if action == "cancel"
                    else batch_generation_commands.pause_batch_generation_task
                )
                command(db, task_id=task_id, authorized_project_id="quota-project-1")
            release_prepare.set()
            worker.join(timeout=30)

        assert not errors
        assert not worker.is_alive()
        with session_factory() as db:
            task = db.get(BatchGenerationTask, task_id)
            item = db.get(BatchGenerationTaskItem, item_id)
            assert task is not None and item is not None
            assert task.status == {"cancel": "canceled", "pause": "paused"}[action]
            assert item.status == {"cancel": "canceled", "pause": "queued"}[action]
            db.delete(item)
            db.delete(task)
            db.commit()


def _assert_postgres_batch_worker_claim_races(session_factory: sessionmaker) -> None:
    task_id = "pg-worker-claim"
    item_id = "pg-worker-claim-item"
    with session_factory() as db:
        db.add(
            BatchGenerationTask(
                id=task_id,
                project_id="quota-project-1",
                outline_id="outline-quota-project-1",
                actor_user_id="quota-user-1",
                runtime_provider="openai",
                status="queued",
                total_count=1,
                params_json='{"context": {}}',
            )
        )
        db.add(BatchGenerationTaskItem(id=item_id, task_id=task_id, chapter_number=1, status="queued"))
        db.commit()

    def _race(claim):  # type: ignore[no-untyped-def]
        barrier = threading.Barrier(2)
        results: list[bool] = []
        errors: list[BaseException] = []

        def _run(index: int) -> None:
            try:
                with session_factory() as db:
                    barrier.wait(timeout=10)
                    results.append(bool(claim(db, index)))
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=_run, args=(1,)), threading.Thread(target=_run, args=(2,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(results) == [False, True]

    def _claim_task(db, _index):  # type: ignore[no-untyped-def]
        claimed = batch_generation_commands.claim_batch_generation_task_for_worker(db, task_id=task_id)
        db.commit()
        return claimed

    _race(_claim_task)
    _race(
        lambda db, index: batch_generation_commands.claim_batch_generation_item_for_worker(
            db, task_id=task_id, item_id=item_id, request_id=f"pg-claim-{index}"
        )
    )
    with session_factory() as db:
        task = db.get(BatchGenerationTask, task_id)
        item = db.get(BatchGenerationTaskItem, item_id)
        assert task is not None and item is not None
        assert task.status == "running"
        assert item.status == "running"
        assert item.attempt_count == 1
        assert item.last_request_id in {"pg-claim-1", "pg-claim-2"}
        db.delete(item)
        db.delete(task)
        db.commit()

    worker_task_id = "pg-worker-e2e-claim"
    worker_item_id = "pg-worker-e2e-item"
    worker_chapter_id = "pg-worker-e2e-chapter"
    project_task_id = "pg-worker-e2e-project-task"
    with session_factory() as db:
        db.add(
            ProjectTask(
                id=project_task_id,
                project_id="quota-project-1",
                actor_user_id="quota-user-1",
                kind="batch_generation",
                status="queued",
                idempotency_key=f"batch_generation:{worker_task_id}",
            )
        )
        db.add(
            Chapter(
                id=worker_chapter_id,
                project_id="quota-project-1",
                outline_id="outline-quota-project-1",
                number=99,
                title="Claim Chapter",
                plan="Claim Plan",
            )
        )
        db.commit()
        db.add(
            BatchGenerationTask(
                id=worker_task_id,
                project_id="quota-project-1",
                outline_id="outline-quota-project-1",
                actor_user_id="quota-user-1",
                project_task_id=project_task_id,
                runtime_provider="openai",
                status="queued",
                total_count=1,
                params_json='{"context": {}}',
            )
        )
        db.commit()
        db.add(
            BatchGenerationTaskItem(
                id=worker_item_id,
                task_id=worker_task_id,
                chapter_id=worker_chapter_id,
                chapter_number=99,
                status="queued",
            )
        )
        db.commit()

    prepare_entered = threading.Event()
    release_prepare = threading.Event()
    prepare_calls: list[str] = []
    llm_calls: list[str] = []
    worker_errors: list[BaseException] = []
    llm_call = batch_generation_service.PreparedLlmCall(
        provider="openai",
        model="test",
        base_url="https://llm.invalid",
        timeout_seconds=10,
        params={},
        params_json="{}",
        extra={},
    )

    def _prepare(**_kwargs):  # type: ignore[no-untyped-def]
        prepare_calls.append("called")
        prepare_entered.set()
        if not release_prepare.wait(timeout=15):
            raise RuntimeError("postgres duplicate worker prepare timed out")
        return (SimpleNamespace(id="quota-project-1"), llm_call, "key", "", "", "", "", "", {})

    def _generate(**_kwargs):  # type: ignore[no-untyped-def]
        llm_calls.append("called")
        return SimpleNamespace(data={"content_md": "Generated", "summary": "Summary"}, run_id=None)

    def _worker() -> None:
        try:
            batch_generation_service.run_batch_generation_task(task_id=worker_task_id)
        except BaseException as exc:  # pragma: no cover - asserted below
            worker_errors.append(exc)

    with (
        patch.object(batch_generation_service, "SessionLocal", session_factory),
        patch.object(batch_generation_service, "_prepare_project_context", side_effect=_prepare),
        patch.object(batch_generation_service, "assemble_chapter_generate_render_values", return_value=({}, {})),
        patch.object(
            batch_generation_service,
            "render_preset_for_task",
            return_value=("", "", [], None, None, None, {}),
        ),
        patch.object(batch_generation_service, "run_chapter_generate_llm_step", side_effect=_generate),
    ):
        winner = threading.Thread(target=_worker)
        winner.start()
        assert prepare_entered.wait(timeout=15)
        duplicate = threading.Thread(target=_worker)
        duplicate.start()
        duplicate.join(timeout=15)
        release_prepare.set()
        winner.join(timeout=30)

    assert not worker_errors
    assert not winner.is_alive() and not duplicate.is_alive()
    assert prepare_calls == ["called"]
    assert llm_calls == ["called"]
    with session_factory() as db:
        item = db.get(BatchGenerationTaskItem, worker_item_id)
        assert item is not None and item.attempt_count == 1 and item.status == "succeeded"
        assert (
            db.query(ProjectTaskEvent)
            .filter(ProjectTaskEvent.task_id == project_task_id, ProjectTaskEvent.event_type == "step_started")
            .count()
            == 1
        )


def _assert_postgres_chapter_replace_transaction(session_factory: sessionmaker) -> None:
    user_id = "chapter-replace-user"
    project_id = "chapter-replace-project"
    outline_id = "chapter-replace-outline"
    other_outline_id = "chapter-replace-other-outline"
    collision_id = "chapter-replace-collision"
    with session_factory() as db:
        db.add(User(id=user_id, display_name="Chapter Replace User"))
        db.commit()
        db.add(Project(id=project_id, owner_user_id=user_id, name="Chapter Replace"))
        db.commit()
        db.add_all(
            [
                Outline(id=outline_id, project_id=project_id, title="Replace Target"),
                Outline(id=other_outline_id, project_id=project_id, title="Collision Source"),
            ]
        )
        db.commit()
        project = db.get(Project, project_id)
        assert project is not None
        project.active_outline_id = outline_id
        db.commit()
        db.add_all(
            [
                Chapter(
                    id="chapter-replace-old",
                    project_id=project_id,
                    outline_id=outline_id,
                    number=9,
                    title="Old title",
                    plan="Old plan",
                    content_md="Old content",
                    summary="Old summary",
                    status="done",
                ),
                Chapter(
                    id=collision_id,
                    project_id=project_id,
                    outline_id=other_outline_id,
                    number=1,
                    title="Collision owner",
                ),
            ]
        )
        db.add(ProjectSettings(project_id=project_id, vector_index_dirty=False))
        db.commit()

    def _snapshot() -> tuple[list[tuple[object, ...]], bool]:
        with session_factory() as db:
            rows = [
                (
                    row.id,
                    row.project_id,
                    row.outline_id,
                    row.number,
                    row.title,
                    row.plan,
                    row.content_md,
                    row.summary,
                    row.status,
                    row.updated_at,
                )
                for row in db.query(Chapter).filter(Chapter.project_id == project_id).order_by(Chapter.id).all()
            ]
            settings_row = db.get(ProjectSettings, project_id)
            assert settings_row is not None
            return rows, bool(settings_row.vector_index_dirty)

    before = _snapshot()
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    request.state.request_id = "postgres-chapter-replace-conflict"
    with (
        patch.object(chapters_routes, "new_id", return_value=collision_id),
        patch.object(chapters_routes, "schedule_vector_rebuild_task") as vector_schedule,
        patch.object(chapters_routes, "schedule_search_rebuild_task") as search_schedule,
        session_factory() as db,
    ):
        with pytest.raises(AppError) as exc_info:
            chapters_routes.bulk_create(
                request=request,
                db=db,
                user_id=user_id,
                project_id=project_id,
                body=BulkCreateRequest(chapters=[{"number": 1, "title": "Must roll back"}]),
                replace=True,
                outline_id=outline_id,
            )
        assert exc_info.value.status_code == 409
        vector_schedule.assert_not_called()
        search_schedule.assert_not_called()
    assert _snapshot() == before

    committed_side_effects: list[str] = []

    def _assert_committed(*, reason: str, **_kwargs: object) -> None:
        assert reason == "chapters_bulk_create"
        with session_factory() as observer:
            rows = (
                observer.query(Chapter)
                .filter(Chapter.project_id == project_id, Chapter.outline_id == outline_id)
                .order_by(Chapter.number)
                .all()
            )
            assert [(row.number, row.title) for row in rows] == [(1, "New one"), (2, "New two")]
            assert {row.id for row in rows} == {"chapter-replace-new-1", "chapter-replace-new-2"}
            settings_row = observer.get(ProjectSettings, project_id)
            assert settings_row is not None and settings_row.vector_index_dirty is True
        committed_side_effects.append(reason)

    request.state.request_id = "postgres-chapter-replace-success"
    with (
        patch.object(chapters_routes, "new_id", side_effect=["chapter-replace-new-1", "chapter-replace-new-2"]),
        patch.object(chapters_routes, "schedule_vector_rebuild_task", side_effect=_assert_committed),
        patch.object(chapters_routes, "schedule_search_rebuild_task", side_effect=_assert_committed),
        session_factory() as db,
    ):
        result = chapters_routes.bulk_create(
            request=request,
            db=db,
            user_id=user_id,
            project_id=project_id,
            body=BulkCreateRequest(chapters=[{"number": 2, "title": "New two"}, {"number": 1, "title": "New one"}]),
            replace=True,
            outline_id=outline_id,
        )
    assert [chapter["number"] for chapter in result["data"]["chapters"]] == [1, 2]
    assert committed_side_effects == ["chapters_bulk_create", "chapters_bulk_create"]


def _assert_postgres_vector_kb_isolation(session_factory: sessionmaker, engine: sa.Engine) -> None:
    user_id = "vector-kb-user"
    project_id = "vector-kb-project"
    other_project_id = "vector-kb-other-project"
    with session_factory() as db:
        db.add(User(id=user_id, display_name="Vector KB User"))
        db.commit()
        db.add_all(
            [
                Project(id=project_id, owner_user_id=user_id, name="Vector KB Project"),
                Project(id=other_project_id, owner_user_id=user_id, name="Other Vector KB Project"),
            ]
        )
        db.commit()

    def _embedding(first: float) -> list[float]:
        return [first, *([0.0] * 1535)]

    def _chunk(chunk_id: str, text_md: str, source_id: str) -> VectorChunk:
        return VectorChunk(
            id=chunk_id,
            text=text_md,
            metadata={"source": "outline", "source_id": source_id, "chunk_index": 0},
        )

    def _vector_rows_snapshot() -> list[tuple[object, ...]]:
        with session_factory() as observer:
            return [
                tuple(row)
                for row in observer.execute(
                    sa.text(
                        """
                        SELECT project_id, kb_id, id, source, source_id, chunk_index, title,
                               chapter_number, text_md, metadata_json, embedding::text,
                               created_at, updated_at,
                               md5(metadata_json || text_md || embedding::text) AS checksum
                        FROM vector_chunks
                        ORDER BY project_id, kb_id, id
                        """
                    )
                ).all()
            ]

    with patch.object(vector_storage, "SessionLocal", session_factory):
        vector_storage._pgvector_upsert_chunks(
            project_id=project_id,
            kb_id="alpha",
            chunks=[_chunk("shared", "alpha dragon", "alpha-shared"), _chunk("alpha-only", "alpha clue", "alpha-only")],
            embeddings=[_embedding(0.0), _embedding(0.1)],
        )
        vector_storage._pgvector_upsert_chunks(
            project_id=project_id,
            kb_id="beta",
            chunks=[_chunk("shared", "beta dragon", "beta-shared"), _chunk("beta-only", "beta clue", "beta-only")],
            embeddings=[_embedding(0.2), _embedding(0.3)],
        )
        vector_storage._pgvector_upsert_chunks(
            project_id=other_project_id,
            kb_id="alpha",
            chunks=[_chunk("shared", "other project secret", "other-shared")],
            embeddings=[_embedding(0.0)],
        )

        alpha = vector_storage._pgvector_hybrid_fetch(
            project_id=project_id,
            kb_id="alpha",
            query_text="alpha",
            query_vec=_embedding(0.0),
            sources=["outline"],
            vector_k=10,
            fts_k=10,
            rrf_k=60,
        )
        assert {candidate["id"] for candidate in alpha["candidates"]} == {"shared", "alpha-only"}
        assert {candidate["metadata"]["kb_id"] for candidate in alpha["candidates"]} == {"alpha"}
        assert all("other project secret" not in candidate["text"] for candidate in alpha["candidates"])

        with (
            patch.object(vector_retrieval, "_prefer_pgvector", return_value=True),
            patch.object(
                vector_retrieval,
                "embed_texts_with_providers",
                return_value={"enabled": True, "vectors": [_embedding(0.0)]},
            ),
            patch.object(vector_storage, "_is_postgres", return_value=True),
            patch.object(settings, "vector_priority_retrieval_enabled", False),
        ):
            queried = vector_retrieval.query_project(
                project_id=project_id,
                kb_ids=["alpha", "beta"],
                query_text="dragon clue",
                sources=["outline"],
                embedding={
                    "provider": "openai",
                    "base_url": "https://embedding.invalid",
                    "model": "test",
                    "api_key": "test",
                },
            )
        assert queried["backend"] == "pgvector"
        assert queried["kbs"]["selected"] == ["alpha", "beta"]
        assert set(queried["kbs"]["per_kb"]) == {"alpha", "beta"}
        assert all(queried["kbs"]["per_kb"][kb_id]["candidate_count"] > 0 for kb_id in ("alpha", "beta"))
        assert {candidate["metadata"]["kb_id"] for candidate in queried["candidates"]} == {"alpha", "beta"}

        before_purge = _vector_rows_snapshot()
        vector_storage._pgvector_delete_project(project_id=project_id, kb_id="alpha")
        assert _vector_rows_snapshot() == [
            row for row in before_purge if not (row[0] == project_id and row[1] == "alpha")
        ]

        beta_before_rebuild = [row for row in _vector_rows_snapshot() if row[0] == project_id and row[1] == "beta"]
        with patch.object(
            vector_storage,
            "embed_texts_with_providers",
            return_value={"enabled": True, "vectors": [_embedding(0.4)]},
        ):
            rebuilt = vector_storage.rebuild_project(
                project_id=project_id,
                kb_id="alpha",
                chunks=[_chunk("alpha-rebuilt", "rebuilt alpha", "alpha-rebuilt")],
                embedding={
                    "provider": "openai",
                    "base_url": "https://embedding.invalid",
                    "model": "test",
                    "api_key": "test",
                },
            )
        assert rebuilt["rebuilt"] == 1
        assert [
            row for row in _vector_rows_snapshot() if row[0] == project_id and row[1] == "beta"
        ] == beta_before_rebuild

        with patch.object(
            vector_storage,
            "embed_texts_with_providers",
            return_value={"enabled": True, "vectors": [[0.0]]},
        ):
            failed = vector_storage.rebuild_project(
                project_id=project_id,
                kb_id="beta",
                chunks=[_chunk("replacement", "must roll back", "replacement")],
                embedding={
                    "provider": "openai",
                    "base_url": "https://embedding.invalid",
                    "model": "test",
                    "api_key": "test",
                },
            )
        assert failed["skipped"] is True
        assert [
            row for row in _vector_rows_snapshot() if row[0] == project_id and row[1] == "beta"
        ] == beta_before_rebuild

    with engine.begin() as connection:
        connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
        plan = connection.execute(
            sa.text(
                "EXPLAIN (FORMAT JSON) SELECT id FROM vector_chunks "
                "WHERE project_id = :pid AND kb_id = 'beta' AND source = 'outline'"
            ),
            {"pid": project_id},
        ).scalar_one()
        assert "ix_vector_chunks_project_kb_source" in str(plan)


def _assert_postgres_migrator_digest_contract(engine: sa.Engine) -> None:
    source_engine = sa.create_engine("sqlite://")
    source_metadata = sa.MetaData()
    target_metadata = sa.MetaData()
    source_table = sa.Table(
        "migrator_digest_contract",
        source_metadata,
        sa.Column("tenant_id", sa.String(16), primary_key=True),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.LargeBinary),
    )
    target_table = sa.Table(
        "migrator_digest_contract",
        target_metadata,
        sa.Column("tenant_id", sa.String(16), primary_key=True),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.LargeBinary),
    )
    rows = [
        {
            "tenant_id": "tenant",
            "id": index,
            "enabled": index % 2 == 0,
            "recorded_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "payload": f"row-{index}".encode(),
        }
        for index in range(1, 22)
    ]
    source_metadata.create_all(source_engine)
    try:
        with engine.begin() as connection:
            target_table.drop(connection, checkfirst=True)
            target_table.create(connection)
        with source_engine.begin() as connection:
            connection.execute(source_table.insert(), rows)
        with source_engine.connect() as source_connection:
            stats = sqlite_pg_migrator._copy_table(
                src_conn=source_connection,
                dst_engine=engine,
                src_table=source_table,
                dst_table=target_table,
                chunk_size=4,
                resume=False,
            )
            with engine.connect() as target_connection:
                assert stats == {"attempted": 21, "inserted": 21, "skipped": 0}
                assert sqlite_pg_migrator._count_rows(source_connection, source_table) == 21
                assert sqlite_pg_migrator._count_rows(target_connection, target_table) == 21
                assert sqlite_pg_migrator._table_digest(
                    source_connection, source_table, canonical_table=target_table, chunk_size=3
                ) == sqlite_pg_migrator._table_digest(
                    target_connection, target_table, canonical_table=target_table, chunk_size=5
                )
            resumed = sqlite_pg_migrator._copy_table(
                src_conn=source_connection,
                dst_engine=engine,
                src_table=source_table,
                dst_table=target_table,
                chunk_size=6,
                resume=True,
            )
            assert resumed == {"attempted": 21, "inserted": 0, "skipped": 21}
            with engine.begin() as target_connection:
                target_connection.execute(
                    sa.update(target_table)
                    .where(target_table.c.tenant_id == "tenant", target_table.c.id == 21)
                    .values(payload=b"resume-drift")
                )
            with engine.connect() as target_connection:
                assert sqlite_pg_migrator._table_digest(
                    source_connection, source_table, canonical_table=target_table, chunk_size=3
                ) != sqlite_pg_migrator._table_digest(
                    target_connection, target_table, canonical_table=target_table, chunk_size=5
                )
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE migrator_fk_parent (tenant_id TEXT NOT NULL, id INTEGER NOT NULL, "
                "PRIMARY KEY (tenant_id, id))"
            )
            connection.exec_driver_sql(
                "CREATE TABLE migrator_fk_child (id INTEGER PRIMARY KEY, tenant_id TEXT, parent_id INTEGER)"
            )
            connection.exec_driver_sql("INSERT INTO migrator_fk_parent VALUES ('tenant', 1)")
            connection.exec_driver_sql("INSERT INTO migrator_fk_child VALUES (1, 'tenant', 1), (2, 'tenant', 2)")
            connection.exec_driver_sql(
                "ALTER TABLE migrator_fk_child ADD CONSTRAINT fk_migrator_composite "
                "FOREIGN KEY (tenant_id, parent_id) REFERENCES migrator_fk_parent(tenant_id, id) NOT VALID"
            )
        fk_metadata = sa.MetaData()
        with engine.connect() as connection:
            fk_metadata.reflect(connection, only=["migrator_fk_parent", "migrator_fk_child"])
            child_table = fk_metadata.tables["migrator_fk_child"]
            fk = sa.inspect(connection).get_foreign_keys("migrator_fk_child")[0]
            assert sqlite_pg_migrator._missing_fk_count(connection, child_table, fk, fk_metadata) == 1
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE IF EXISTS migrator_fk_child CASCADE")
            connection.exec_driver_sql("DROP TABLE IF EXISTS migrator_fk_parent CASCADE")
            target_table.drop(connection, checkfirst=True)
        source_engine.dispose()


def _assert_postgres_migrator_main_contract(engine: sa.Engine) -> None:
    schema_name = "migrator_main_contract"
    with tempfile.TemporaryDirectory() as temp_dir:
        source_path = Path(temp_dir) / "source.db"
        report_path = Path(temp_dir) / "report.json"
        source_engine = sa.create_engine(f"sqlite:///{source_path.as_posix()}")
        metadata = sa.MetaData()
        users = sa.Table(
            "users",
            metadata,
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("name", sa.String, nullable=False),
        )
        projects = sa.Table(
            "projects",
            metadata,
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("name", sa.String, nullable=False),
            sa.Column("active_outline_id", sa.String, nullable=True),
        )
        parents = sa.Table(
            "parents",
            metadata,
            sa.Column("tenant_id", sa.String, primary_key=True),
            sa.Column("id", sa.Integer, primary_key=True),
        )
        children = sa.Table(
            "children",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("tenant_id", sa.String),
            sa.Column("parent_id", sa.Integer),
        )
        moments = sa.Table(
            "moments",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        )
        metadata.create_all(source_engine)
        with source_engine.begin() as connection:
            connection.execute(users.insert(), {"id": "user-1", "name": "Source User"})
            connection.execute(projects.insert(), {"id": "project-1", "name": "Source Project"})
            connection.execute(parents.insert(), {"tenant_id": "tenant", "id": 1})
            connection.execute(
                children.insert(),
                [
                    {"id": 1, "tenant_id": "tenant", "parent_id": 1},
                    {"id": 2, "tenant_id": "tenant", "parent_id": 2},
                ],
            )
            connection.execute(moments.insert(), {"id": 1, "occurred_at": datetime(2026, 1, 1, 12, 0, 0)})
        source_engine.dispose()

        with engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema_name}"')
            connection.exec_driver_sql(f'CREATE TABLE "{schema_name}".users (id TEXT PRIMARY KEY, name TEXT NOT NULL)')
            connection.exec_driver_sql(
                f'CREATE TABLE "{schema_name}".projects '
                "(id TEXT PRIMARY KEY, name TEXT NOT NULL, active_outline_id TEXT)"
            )
            connection.exec_driver_sql(
                f'CREATE TABLE "{schema_name}".parents '
                "(tenant_id TEXT NOT NULL, id INTEGER NOT NULL, PRIMARY KEY (tenant_id, id))"
            )
            connection.exec_driver_sql(
                f'CREATE TABLE "{schema_name}".children (id INTEGER PRIMARY KEY, tenant_id TEXT, parent_id INTEGER)'
            )
            connection.exec_driver_sql(
                f'CREATE TABLE "{schema_name}".moments (id INTEGER PRIMARY KEY, occurred_at TIMESTAMPTZ NOT NULL)'
            )
        target_url = engine.url.update_query_dict(
            {"options": f"-csearch_path={schema_name} -ctimezone=Asia/Hong_Kong"}
        ).render_as_string(hide_password=False)
        argv = [
            "--source",
            str(source_path),
            "--target",
            target_url,
            "--no-migrate-schema",
            "--report",
            str(report_path),
        ]
        try:
            assert sqlite_pg_migrator.main(argv) == 0
            succeeded = json.loads(report_path.read_text(encoding="utf-8"))
            assert succeeded["status"] == "succeeded"
            assert succeeded["verification"]["failures"] == []
            target_engine = sa.create_engine(target_url)
            try:
                with target_engine.connect() as connection:
                    stored = connection.exec_driver_sql("SELECT occurred_at FROM moments WHERE id = 1").scalar_one()
                    assert stored.astimezone(timezone.utc) == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
            finally:
                target_engine.dispose()

            with engine.begin() as connection:
                connection.exec_driver_sql(
                    f"UPDATE \"{schema_name}\".users SET name = 'Target Drift' WHERE id = 'user-1'"
                )
            assert sqlite_pg_migrator.main([*argv, "--resume"]) == 1
            failed = json.loads(report_path.read_text(encoding="utf-8"))
            assert failed["status"] == "failed"
            assert failed["verification"]["status"] == "failed"
            assert {failure["check"] for failure in failed["verification"]["failures"]} == {"digest"}

            with engine.begin() as connection:
                connection.exec_driver_sql(
                    f"UPDATE \"{schema_name}\".users SET name = 'Source User' WHERE id = 'user-1'"
                )
                connection.exec_driver_sql(
                    f"INSERT INTO \"{schema_name}\".users (id, name) VALUES ('extra-user', 'Extra')"
                )
            assert sqlite_pg_migrator.main([*argv, "--resume"]) == 1
            count_failed = json.loads(report_path.read_text(encoding="utf-8"))
            assert "count" in {failure["check"] for failure in count_failed["verification"]["failures"]}

            with engine.begin() as connection:
                connection.exec_driver_sql(f"DELETE FROM \"{schema_name}\".users WHERE id = 'extra-user'")
                connection.exec_driver_sql(
                    f'ALTER TABLE "{schema_name}".children ADD CONSTRAINT fk_main_composite '
                    f'FOREIGN KEY (tenant_id, parent_id) REFERENCES "{schema_name}".parents(tenant_id, id) NOT VALID'
                )
            assert sqlite_pg_migrator.main([*argv, "--resume"]) == 1
            fk_failed = json.loads(report_path.read_text(encoding="utf-8"))
            assert {failure["check"] for failure in fk_failed["verification"]["failures"]} == {"foreign_keys"}
        finally:
            with engine.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')


@pytest.mark.postgres_integration
def test_pgvector_cleanup_reconciliation_and_alembic_contract() -> None:
    """Exercise the real PostgreSQL-only DDL and the two current migrations."""
    database_url = _postgres_url()
    engine = sa.create_engine(database_url)
    destructive_target_verified = False
    try:
        # This test is destructive by design and only runs against the isolated
        # CI service when explicitly enabled.
        with engine.connect() as connection:
            actual_database = str(connection.exec_driver_sql("SELECT current_database()").scalar_one())
            if actual_database != EXPECTED_DATABASE:
                raise RuntimeError(
                    f"connected database {actual_database!r} does not match destructive target {EXPECTED_DATABASE!r}"
                )
            destructive_target_verified = True
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA public")
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS rogue_contract CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA rogue_contract")
            connection.exec_driver_sql("CREATE DOMAIN public.rogue_domain AS TEXT")

        with pytest.raises(SchemaContractError, match=r"non_public_schemas.*rogue_contract.*user_types.*rogue_domain"):
            check_empty_database(database_url)

        with engine.begin() as connection:
            connection.exec_driver_sql("DROP SCHEMA rogue_contract CASCADE")
            connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA public")

        _upgrade(database_url, PRE_CLEANUP_REVISION)
        with engine.begin() as connection:
            tables = set(sa.inspect(connection).get_table_names())
            assert RETIRED_TABLES.issubset(tables)
            actor_user_id = next(
                column
                for column in sa.inspect(connection).get_columns("batch_generation_tasks")
                if column["name"] == "actor_user_id"
            )
            assert isinstance(actor_user_id["type"], sa.String)
            assert actor_user_id["type"].length == 36
            connection.execute(
                sa.text(
                    "INSERT INTO users (id, display_name, created_at, updated_at, is_admin) "
                    "VALUES ('legacy-vector-user', 'Legacy Vector User', now(), now(), false)"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO projects (id, owner_user_id, name, created_at, updated_at) "
                    "VALUES ('legacy-vector-project', 'legacy-vector-user', 'Legacy Vector Project', now(), now())"
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO vector_chunks (
                        id, project_id, source, source_id, chunk_index, title,
                        chapter_number, text_md, metadata_json, embedding
                    ) VALUES (
                        'legacy-vector-chunk', 'legacy-vector-project', 'outline', 'legacy-outline', 3,
                        'Legacy title', 7, 'Legacy vector text', '{"legacy": true}', (:embedding)::vector
                    )
                    """
                ),
                {"embedding": vector_storage._pgvector_literal([0.25, *([0.0] * 1535)])},
            )
            legacy_vector_snapshot = connection.execute(
                sa.text(
                    """
                    SELECT id, project_id, source, source_id, chunk_index, title, chapter_number,
                           text_md, metadata_json, embedding::text, created_at, updated_at
                    FROM vector_chunks WHERE id = 'legacy-vector-chunk'
                    """
                )
            ).one()

        _upgrade(database_url, "head")
        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            tables = set(inspector.get_table_names())
            assert tables.isdisjoint(RETIRED_TABLES)
            assert "vector_chunks" in tables
            vector_columns = {column["name"]: column for column in inspector.get_columns("vector_chunks")}
            assert vector_columns["kb_id"]["nullable"] is False
            assert vector_columns["kb_id"]["type"].length == 64
            assert "default" in str(vector_columns["kb_id"]["default"])
            assert inspector.get_pk_constraint("vector_chunks")["constrained_columns"] == ["project_id", "kb_id", "id"]
            vector_indexes = {str(index["name"]) for index in inspector.get_indexes("vector_chunks")}
            assert {
                "ix_vector_chunks_project_kb",
                "ix_vector_chunks_project_kb_source",
                "ix_vector_chunks_content_tsv",
                "ix_vector_chunks_embedding_ivfflat",
            }.issubset(vector_indexes)
            migrated_legacy = connection.execute(
                sa.text(
                    """
                    SELECT id, project_id, source, source_id, chunk_index, title, chapter_number,
                           text_md, metadata_json, embedding::text, created_at, updated_at, kb_id
                    FROM vector_chunks WHERE id = 'legacy-vector-chunk'
                    """
                )
            ).one()
            assert tuple(migrated_legacy[:-1]) == tuple(legacy_vector_snapshot)
            assert migrated_legacy[-1] == "default"
            actor_user_id = next(
                column
                for column in inspector.get_columns("batch_generation_tasks")
                if column["name"] == "actor_user_id"
            )
            assert actor_user_id["type"].length == 64
            runtime_provider = next(
                column
                for column in inspector.get_columns("batch_generation_tasks")
                if column["name"] == "runtime_provider"
            )
            assert runtime_provider["type"].length == 64
            assert "batch_generation_quota_guards" in tables
            batch_indexes = {str(index["name"]) for index in inspector.get_indexes("batch_generation_tasks")}
            assert {
                "ix_batch_generation_tasks_project_status",
                "ix_batch_generation_tasks_actor_status",
                "ix_batch_generation_tasks_provider_status",
            }.issubset(batch_indexes)
            is_admin = next(column for column in inspector.get_columns("users") if column["name"] == "is_admin")
            assert is_admin["default"] is not None
            current = tuple(MigrationContext.configure(connection).get_current_heads())
            assert current == (HEAD_REVISION,)
            assert migration_head(database_url) == HEAD_REVISION
            # Programmatic equivalent of ``alembic check`` with the same
            # compare-type/default and dialect-specific unmanaged-table filter.
            assert_schema_matches_metadata(connection)

        _downgrade(database_url, "f3a1c7e9b2d4")
        with engine.connect() as connection:
            assert "kb_id" not in {column["name"] for column in sa.inspect(connection).get_columns("vector_chunks")}
            downgraded_legacy = connection.execute(
                sa.text(
                    """
                    SELECT id, project_id, source, source_id, chunk_index, title, chapter_number,
                           text_md, metadata_json, embedding::text, created_at, updated_at
                    FROM vector_chunks WHERE id = 'legacy-vector-chunk'
                    """
                )
            ).one()
            assert tuple(downgraded_legacy) == tuple(legacy_vector_snapshot)
        _upgrade(database_url, "head")

        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO vector_chunks (
                        id, project_id, kb_id, source, source_id, chunk_index, title,
                        chapter_number, text_md, metadata_json, embedding
                    ) VALUES (
                        'nondefault-vector-chunk', 'legacy-vector-project', 'secondary', 'outline',
                        'legacy-outline', 0, NULL, NULL, 'nondefault', '{}', (:embedding)::vector
                    )
                    """
                ),
                {"embedding": vector_storage._pgvector_literal([0.5, *([0.0] * 1535)])},
            )
        with pytest.raises(RuntimeError, match="non-default knowledge-base data"):
            _downgrade(database_url, "f3a1c7e9b2d4")
        with engine.begin() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == HEAD_REVISION
            connection.execute(sa.text("DELETE FROM vector_chunks WHERE id = 'nondefault-vector-chunk'"))
            connection.execute(
                sa.text(
                    "INSERT INTO users (id, display_name, created_at, updated_at, is_admin) "
                    "VALUES ('duplicate-vector-user', 'Duplicate Vector User', now(), now(), false)"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO projects (id, owner_user_id, name, created_at, updated_at) "
                    "VALUES ('duplicate-vector-project', 'duplicate-vector-user', "
                    "'Duplicate Vector Project', now(), now())"
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO vector_chunks (
                        id, project_id, kb_id, source, source_id, chunk_index, title,
                        chapter_number, text_md, metadata_json, embedding
                    ) VALUES (
                        'legacy-vector-chunk', 'duplicate-vector-project', 'default', 'outline',
                        'duplicate-outline', 0, NULL, NULL, 'duplicate', '{}', (:embedding)::vector
                    )
                    """
                ),
                {"embedding": vector_storage._pgvector_literal([0.75, *([0.0] * 1535)])},
            )
        with pytest.raises(RuntimeError, match="duplicated across projects"):
            _downgrade(database_url, "f3a1c7e9b2d4")
        with engine.begin() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == HEAD_REVISION
            connection.execute(sa.text("DELETE FROM projects WHERE id = 'duplicate-vector-project'"))
            connection.execute(sa.text("DELETE FROM users WHERE id = 'duplicate-vector-user'"))
        # Also execute Alembic's public command path so env.py itself is part
        # of the PostgreSQL contract, rather than only the shared primitives.
        _alembic_check(database_url)

        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        with session_factory() as db:
            db.add(User(id="prompt-sync-user", display_name="Prompt Sync"))
            db.commit()
            db.add(
                Project(
                    id="prompt-sync-project",
                    owner_user_id="prompt-sync-user",
                    name="Prompt Sync Project",
                    genre=None,
                    logline=None,
                )
            )
            db.commit()

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _sync_concurrently() -> None:
            try:
                with session_factory() as db:
                    barrier.wait(timeout=10)
                    sync_builtin_prompt_defaults(db, project_id="prompt-sync-project")
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=_sync_concurrently), threading.Thread(target=_sync_concurrently)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        with session_factory() as db:
            rows = db.query(PromptPreset).filter(PromptPreset.project_id == "prompt-sync-project").all()
            assert len(rows) == 6
            assert len({row.resource_key for row in rows}) == 6

        with session_factory() as db:
            db.add_all(
                [
                    User(id="quota-user-1", display_name="Quota User 1"),
                    User(id="quota-user-2", display_name="Quota User 2"),
                ]
            )
            db.commit()
            db.add_all(
                [
                    Project(
                        id="quota-project-1",
                        owner_user_id="quota-user-1",
                        name="Quota Project 1",
                        genre=None,
                        logline=None,
                    ),
                    Project(
                        id="quota-create-project",
                        owner_user_id="quota-user-1",
                        name="Quota Create Project",
                        genre=None,
                        logline=None,
                    ),
                    Project(
                        id="quota-project-2",
                        owner_user_id="quota-user-2",
                        name="Quota Project 2",
                        genre=None,
                        logline=None,
                    ),
                ]
            )
            db.commit()
            db.add(LLMPreset(project_id="quota-create-project", provider="openai", model="gpt-4o-mini"))
            db.commit()
            db.add_all(
                [
                    Outline(id="outline-quota-project-1", project_id="quota-project-1", title="Quota Outline 1"),
                    Outline(id="outline-quota-project-2", project_id="quota-project-2", title="Quota Outline 2"),
                ]
            )
            db.commit()

        _assert_postgres_create_route_race(session_factory)
        _assert_postgres_batch_command_races(session_factory)
        _assert_postgres_worker_control_races(session_factory)
        _assert_postgres_batch_worker_claim_races(session_factory)
        _assert_postgres_chapter_replace_transaction(session_factory)
        _assert_postgres_vector_kb_isolation(session_factory, engine)
        _assert_postgres_migrator_digest_contract(engine)
        _assert_postgres_migrator_main_contract(engine)
        _assert_postgres_quota_race(
            session_factory,
            dimension="project",
            first=("quota-project-1", "quota-user-1", "openai"),
            second=("quota-project-1", "quota-user-2", "anthropic"),
            limits=(1, 10, 10),
        )
        _assert_postgres_quota_race(
            session_factory,
            dimension="user",
            first=("quota-project-1", "quota-user-1", "openai"),
            second=("quota-project-2", "quota-user-1", "anthropic"),
            limits=(10, 1, 10),
        )
        _assert_postgres_quota_race(
            session_factory,
            dimension="provider",
            first=("quota-project-1", "quota-user-1", "openai"),
            second=("quota-project-2", "quota-user-2", "openai"),
            limits=(10, 10, 1),
        )

        explain_cases = (
            (
                "ix_batch_generation_tasks_project_status",
                "project_id",
                "quota-project-1",
            ),
            (
                "ix_batch_generation_tasks_actor_status",
                "actor_user_id",
                "quota-user-1",
            ),
            (
                "ix_batch_generation_tasks_provider_status",
                "runtime_provider",
                "openai",
            ),
        )
        with engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
            for index_name, column_name, scope_key in explain_cases:
                plan = connection.execute(
                    sa.text(
                        f"""
                        EXPLAIN (FORMAT JSON)
                        SELECT COUNT(*)
                        FROM batch_generation_tasks
                        WHERE {column_name} = :scope_key
                          AND status IN ('queued', 'running', 'paused')
                        """
                    ),
                    {"scope_key": scope_key},
                ).scalar_one()
                assert index_name in str(plan)
    finally:
        if destructive_target_verified:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
                connection.exec_driver_sql("DROP SCHEMA IF EXISTS rogue_contract CASCADE")
        engine.dispose()
