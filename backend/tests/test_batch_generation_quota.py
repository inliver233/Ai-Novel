from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from starlette.requests import Request
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes import batch_generation as batch_generation_routes
from app.core.config import settings
from app.core.errors import AppError
from app.db.base import Base
from app.models.batch_generation_task import BatchGenerationQuotaGuard, BatchGenerationTask
from app.models.chapter import Chapter
from app.models.llm_preset import LLMPreset
from app.models.project import Project
from app.models.user import User
from app.schemas.batch_generation import BatchGenerationCreateRequest
from app.services.batch_generation_quota import (
    enter_batch_generation_quota_admission,
    lock_and_enforce_batch_generation_quotas,
)


def _session_factory(database_path: Path) -> tuple[sa.Engine, sessionmaker[Session]]:
    engine = sa.create_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _task(*, task_id: str, project_id: str, user_id: str, provider: str, status: str = "queued") -> BatchGenerationTask:
    return BatchGenerationTask(
        id=task_id,
        project_id=project_id,
        outline_id=f"outline-{project_id}",
        actor_user_id=user_id,
        runtime_provider=provider,
        status=status,
    )


@pytest.mark.parametrize(
    ("dimension", "first", "second", "limits"),
    [
        ("project", ("p1", "u1", "openai"), ("p1", "u2", "anthropic"), (1, 10, 10)),
        ("user", ("p1", "u1", "openai"), ("p2", "u1", "anthropic"), (10, 1, 10)),
        ("provider", ("p1", "u1", "openai"), ("p2", "u2", "openai"), (10, 10, 1)),
    ],
)
def test_sqlite_quota_guard_serializes_concurrent_admission(
    tmp_path: Path,
    dimension: str,
    first: tuple[str, str, str],
    second: tuple[str, str, str],
    limits: tuple[int, int, int],
) -> None:
    engine, session_factory = _session_factory(tmp_path / f"quota-{dimension}.db")
    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []

    def _admit(index: int, values: tuple[str, str, str]) -> None:
        project_id, user_id, provider = values
        try:
            with session_factory() as db:
                db.execute(sa.select(BatchGenerationTask.id)).all()
                barrier.wait(timeout=10)
                enter_batch_generation_quota_admission(db)
                lock_and_enforce_batch_generation_quotas(
                    db,
                    project_id=project_id,
                    user_id=user_id,
                    provider=provider,
                )
                db.add(
                    _task(
                        task_id=f"task-{index}",
                        project_id=project_id,
                        user_id=user_id,
                        provider=provider,
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
        assert db.query(BatchGenerationQuotaGuard).count() >= 3
    engine.dispose()


@pytest.mark.parametrize(
    ("expected_quota", "limits"),
    [
        ("project", (1, 10, 10)),
        ("user", (10, 1, 10)),
        ("provider", (10, 10, 1)),
    ],
)
def test_quota_error_is_precise_and_paused_tasks_remain_active(
    tmp_path: Path,
    expected_quota: str,
    limits: tuple[int, int, int],
) -> None:
    engine, session_factory = _session_factory(tmp_path / f"precise-{expected_quota}.db")
    with session_factory() as db:
        db.add(_task(task_id="existing", project_id="p1", user_id="u1", provider="openai", status="paused"))
        db.commit()

    project_limit, user_limit, provider_limit = limits
    with (
        patch.object(settings, "batch_generation_project_active_limit", project_limit),
        patch.object(settings, "batch_generation_user_active_limit", user_limit),
        patch.object(settings, "batch_generation_provider_active_limit", provider_limit),
        session_factory() as db,
    ):
        with pytest.raises(AppError) as error:
            enter_batch_generation_quota_admission(db)
            lock_and_enforce_batch_generation_quotas(
                db,
                project_id="p1",
                user_id="u1",
                provider="openai",
            )

    assert error.value.code == "BATCH_GENERATION_QUOTA_EXCEEDED"
    assert error.value.status_code == 409
    assert error.value.details == {
        "quota": expected_quota,
        "scope_key": {"project": "p1", "user": "u1", "provider": "openai"}[expected_quota],
        "active_count": 1,
        "limit": 1,
    }
    engine.dispose()


def test_ignore_task_id_excludes_only_the_same_active_task(tmp_path: Path) -> None:
    engine, session_factory = _session_factory(tmp_path / "ignore-self.db")
    with session_factory() as db:
        db.add(_task(task_id="self", project_id="p1", user_id="u1", provider="openai"))
        db.commit()

    with (
        patch.object(settings, "batch_generation_project_active_limit", 1),
        patch.object(settings, "batch_generation_user_active_limit", 1),
        patch.object(settings, "batch_generation_provider_active_limit", 1),
        session_factory() as db,
    ):
        enter_batch_generation_quota_admission(db)
        lock_and_enforce_batch_generation_quotas(
            db,
            project_id="p1",
            user_id="u1",
            provider="openai",
            ignore_task_id="self",
        )
        db.rollback()

    with (
        patch.object(settings, "batch_generation_project_active_limit", 1),
        patch.object(settings, "batch_generation_user_active_limit", 1),
        patch.object(settings, "batch_generation_provider_active_limit", 1),
        session_factory() as db,
    ):
        with pytest.raises(AppError, match="当前项目"):
            enter_batch_generation_quota_admission(db)
            lock_and_enforce_batch_generation_quotas(
                db,
                project_id="p1",
                user_id="u1",
                provider="openai",
                ignore_task_id="different-task",
            )
    engine.dispose()


def test_quota_counts_use_sql_aggregates_without_loading_task_payloads(tmp_path: Path) -> None:
    engine, session_factory = _session_factory(tmp_path / "sql-count.db")
    statements: list[str] = []

    def _capture_sql(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:  # type: ignore[no-untyped-def]
        statements.append(str(statement).lower())

    event.listen(engine, "before_cursor_execute", _capture_sql)
    try:
        with session_factory() as db:
            enter_batch_generation_quota_admission(db)
            lock_and_enforce_batch_generation_quotas(
                db,
                project_id="p1",
                user_id="u1",
                provider="openai",
            )
            db.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", _capture_sql)

    task_selects = [statement for statement in statements if "from batch_generation_tasks" in statement]
    assert len(task_selects) == 3
    assert all("count(" in statement for statement in task_selects)
    assert all("params_json" not in statement and "error_json" not in statement for statement in task_selects)
    engine.dispose()


def test_admission_boundary_refuses_to_rollback_pending_mutations(tmp_path: Path) -> None:
    engine, session_factory = _session_factory(tmp_path / "pending-mutation.db")
    with session_factory() as db:
        pending = _task(task_id="pending", project_id="p1", user_id="u1", provider="openai")
        db.add(pending)
        with pytest.raises(RuntimeError, match="before pending ORM mutations"):
            enter_batch_generation_quota_admission(db)
        assert pending in db.new
    engine.dispose()


def test_sqlite_create_route_serializes_after_default_outline_commit(tmp_path: Path) -> None:
    engine, session_factory = _session_factory(tmp_path / "route-default-outline.db")
    with session_factory() as db:
        db.add(User(id="owner", display_name="Owner"))
        db.add(Project(id="project", owner_user_id="owner", name="Project"))
        db.add(LLMPreset(project_id="project", provider="openai", model="gpt-4o-mini"))
        db.commit()

    before_outline = threading.Barrier(2)
    after_chapter = threading.Barrier(2)
    outline_write_lock = threading.Lock()
    results: list[str] = []
    errors: list[BaseException] = []
    original_ensure = batch_generation_routes.ensure_active_outline

    def _ensure_with_candidate(db: Session, *, project: Project):
        before_outline.wait(timeout=10)
        with outline_write_lock:
            outline = original_ensure(db, project=project)
            chapter_id = f"chapter-{outline.id}"
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
                request.state.request_id = f"request-{index}"
                batch_generation_routes.create_batch_generation_task(
                    request=request,
                    db=db,
                    user_id="owner",
                    project_id="project",
                    body=BatchGenerationCreateRequest(count=1, include_existing=True),
                )
                results.append("admitted")
        except AppError as exc:
            results.append(str(exc.details.get("quota")))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    with (
        patch.object(batch_generation_routes, "ensure_active_outline", side_effect=_ensure_with_candidate),
        patch.object(batch_generation_routes, "get_task_queue", return_value=_NoopQueue()),
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
        assert db.query(BatchGenerationTask).count() == 1
    engine.dispose()
