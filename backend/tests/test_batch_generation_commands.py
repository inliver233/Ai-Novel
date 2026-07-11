from __future__ import annotations

import ast
import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes import batch_generation as batch_generation_routes
from app.core.errors import AppError
from app.db.base import Base
from app.models.batch_generation_task import BatchGenerationTask, BatchGenerationTaskItem
from app.models.project_task import ProjectTask
from app.models.project_task_event import ProjectTaskEvent
from app.services import batch_generation_commands


class _NoopQueue:
    def enqueue_batch_generation_task(self, task_id: str) -> str:
        return task_id


def _session_factory(database_path: Path) -> tuple[sa.Engine, sessionmaker[Session]]:
    engine = sa.create_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _seed_task(
    db: Session,
    *,
    task_id: str,
    status: str,
    failed_item: bool = False,
    project_task: bool = False,
) -> None:
    project_task_id = f"runtime-{task_id}" if project_task else None
    if project_task_id is not None:
        db.add(
            ProjectTask(
                id=project_task_id,
                project_id="project",
                actor_user_id="owner",
                kind="batch_generation_orchestrator",
                status=status,
                idempotency_key=f"batch_generation:{task_id}",
            )
        )
    db.add(
        BatchGenerationTask(
            id=task_id,
            project_id="project",
            outline_id="outline",
            actor_user_id="owner",
            project_task_id=project_task_id,
            runtime_provider="openai",
            status=status,
            total_count=1,
            failed_count=1 if failed_item else 0,
            pause_requested=status == "paused",
        )
    )
    db.add(
        BatchGenerationTaskItem(
            id=f"item-{task_id}",
            task_id=task_id,
            chapter_id=None,
            chapter_number=1,
            status="failed" if failed_item else "queued",
            error_message="boom" if failed_item else None,
        )
    )
    db.commit()


def _invoke(db: Session, *, action: str, task_id: str):  # type: ignore[no-untyped-def]
    kwargs = {
        "db": db,
        "task_id": task_id,
        "authorized_project_id": "project",
    }
    if action in {"resume", "retry_failed", "skip_failed"}:
        kwargs["actor_user_id"] = "owner"
    command = {
        "pause": batch_generation_commands.pause_batch_generation_task,
        "resume": batch_generation_commands.resume_batch_generation_task,
        "retry_failed": batch_generation_commands.retry_failed_batch_generation_task,
        "skip_failed": batch_generation_commands.skip_failed_batch_generation_task,
        "cancel": batch_generation_commands.cancel_batch_generation_task,
    }[action]
    return command(**kwargs)


@pytest.mark.parametrize("action", ["pause", "resume", "retry_failed", "skip_failed", "cancel"])
@pytest.mark.parametrize("status", ["queued", "running", "paused", "succeeded", "failed", "canceled"])
def test_action_status_matrix_is_owned_by_command_service(
    tmp_path: Path,
    action: str,
    status: str,
) -> None:
    engine, session_factory = _session_factory(tmp_path / f"matrix-{action}-{status}.db")
    task_id = f"{action}-{status}"
    needs_failed_item = action in {"retry_failed", "skip_failed"} and status == "paused"
    with session_factory() as db:
        _seed_task(db, task_id=task_id, status=status, failed_item=needs_failed_item)

    expected_applied = status in batch_generation_commands.BATCH_GENERATION_TRANSITIONS[action]
    expected_status = {
        ("pause", "queued"): "paused",
        ("pause", "running"): "running",
        ("resume", "paused"): "queued",
        ("retry_failed", "paused"): "queued",
        ("skip_failed", "paused"): "succeeded",
        ("cancel", "queued"): "canceled",
        ("cancel", "running"): "running",
        ("cancel", "paused"): "canceled",
    }.get((action, status), status)

    with (
        patch.object(batch_generation_commands, "get_task_queue", return_value=_NoopQueue()),
        session_factory() as db,
    ):
        result = _invoke(db, action=action, task_id=task_id)

    assert result.applied is expected_applied
    assert result.snapshot.task.status == expected_status
    if status in batch_generation_commands.TERMINAL_BATCH_GENERATION_STATUSES:
        assert result.snapshot.task.status == status
    engine.dispose()


@pytest.mark.parametrize(
    ("action", "initial_status", "failed_item"),
    [
        ("pause", "queued", False),
        ("resume", "paused", False),
        ("retry_failed", "paused", True),
        ("skip_failed", "paused", True),
        ("cancel", "queued", False),
    ],
)
def test_repeated_action_is_idempotent(
    tmp_path: Path,
    action: str,
    initial_status: str,
    failed_item: bool,
) -> None:
    engine, session_factory = _session_factory(tmp_path / f"idempotent-{action}.db")
    with session_factory() as db:
        _seed_task(db, task_id="task", status=initial_status, failed_item=failed_item)

    with patch.object(batch_generation_commands, "get_task_queue", return_value=_NoopQueue()):
        with session_factory() as db:
            first = _invoke(db, action=action, task_id="task")
        with session_factory() as db:
            second = _invoke(db, action=action, task_id="task")

    assert first.applied is True
    assert second.applied is False
    assert second.snapshot.task.status == first.snapshot.task.status
    engine.dispose()


@pytest.mark.parametrize("competing_action", ["resume", "retry_failed"])
def test_sqlite_cancel_wins_race_without_terminal_regression(
    tmp_path: Path,
    competing_action: str,
) -> None:
    engine, session_factory = _session_factory(tmp_path / f"cancel-{competing_action}.db")
    with session_factory() as db:
        _seed_task(
            db,
            task_id="task",
            status="paused",
            failed_item=competing_action == "retry_failed",
        )

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _run(action: str) -> None:
        try:
            with session_factory() as db:
                barrier.wait(timeout=10)
                _invoke(db, action=action, task_id="task")
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
        task = db.get(BatchGenerationTask, "task")
        item = db.get(BatchGenerationTaskItem, "item-task")
        assert task is not None
        assert item is not None
        assert task.status == "canceled"
        assert task.cancel_requested is True
        assert item.status != "queued"
    engine.dispose()


@pytest.mark.parametrize(
    ("queue_error", "expected_code"),
    [
        (AppError(code="QUEUE_UNAVAILABLE", message="queue down", status_code=503), "QUEUE_UNAVAILABLE"),
        (RuntimeError("queue down"), "QUEUE_ENQUEUE_ERROR"),
    ],
)
def test_resume_enqueue_failure_restores_paused_checkpoint(
    tmp_path: Path,
    queue_error: Exception,
    expected_code: str,
) -> None:
    engine, session_factory = _session_factory(tmp_path / f"enqueue-{expected_code}.db")
    with session_factory() as db:
        _seed_task(db, task_id="task", status="paused")

    class _FailingQueue:
        def enqueue_batch_generation_task(self, _task_id: str) -> str:
            raise queue_error

    with (
        patch.object(batch_generation_commands, "get_task_queue", return_value=_FailingQueue()),
        session_factory() as db,
        pytest.raises(AppError) as error,
    ):
        batch_generation_commands.resume_batch_generation_task(
            db,
            task_id="task",
            authorized_project_id="project",
            actor_user_id="owner",
        )

    assert error.value.code == expected_code
    with session_factory() as db:
        task = db.get(BatchGenerationTask, "task")
        assert task is not None
        assert task.status == "paused"
        assert task.pause_requested is True
        assert expected_code in str(task.error_json)
    engine.dispose()


def test_enqueue_compensation_cannot_regress_concurrent_cancel(
    tmp_path: Path,
) -> None:
    engine, session_factory = _session_factory(tmp_path / "enqueue-cancel.db")
    with session_factory() as db:
        _seed_task(db, task_id="task", status="paused")

    class _CancelThenFailQueue:
        def enqueue_batch_generation_task(self, task_id: str) -> str:
            with session_factory() as cancel_db:
                batch_generation_commands.cancel_batch_generation_task(
                    cancel_db,
                    task_id=task_id,
                    authorized_project_id="project",
                )
            raise RuntimeError("queue down")

    with (
        patch.object(batch_generation_commands, "get_task_queue", return_value=_CancelThenFailQueue()),
        session_factory() as db,
        pytest.raises(AppError, match="批量生成任务入队失败"),
    ):
        batch_generation_commands.resume_batch_generation_task(
            db,
            task_id="task",
            authorized_project_id="project",
            actor_user_id="owner",
        )

    with session_factory() as db:
        task = db.get(BatchGenerationTask, "task")
        item = db.get(BatchGenerationTaskItem, "item-task")
        assert task is not None
        assert item is not None
        assert task.status == "canceled"
        assert task.cancel_requested is True
        assert item.status == "canceled"
    engine.dispose()


def test_create_enqueue_failure_marks_only_owned_queued_state_failed(
    tmp_path: Path,
) -> None:
    engine, session_factory = _session_factory(tmp_path / "create-enqueue.db")

    class _FailingQueue:
        def enqueue_batch_generation_task(self, _task_id: str) -> str:
            raise AppError(code="QUEUE_UNAVAILABLE", message="queue down", status_code=503)

    with (
        patch.object(batch_generation_commands, "get_task_queue", return_value=_FailingQueue()),
        session_factory() as db,
        pytest.raises(AppError) as error,
    ):
        batch_generation_commands.create_admitted_batch_generation_task(
            db,
            project_id="project",
            outline_id="outline",
            actor_user_id="owner",
            runtime_provider="openai",
            params_json="{}",
            chapter_refs=[("chapter", 1)],
            request_id="request",
        )

    assert error.value.code == "QUEUE_UNAVAILABLE"
    with session_factory() as db:
        task = db.execute(sa.select(BatchGenerationTask)).scalar_one()
        item = db.execute(sa.select(BatchGenerationTaskItem)).scalar_one()
        assert task.status == "failed"
        assert task.failed_count == 1
        assert item.status == "failed"
        assert "QUEUE_UNAVAILABLE" in str(task.error_json)
    engine.dispose()


def test_create_enqueue_failure_cannot_regress_queue_side_effect_cancel(
    tmp_path: Path,
) -> None:
    engine, session_factory = _session_factory(tmp_path / "create-enqueue-cancel.db")

    class _CancelThenFailQueue:
        def enqueue_batch_generation_task(self, task_id: str) -> str:
            with session_factory() as cancel_db:
                batch_generation_commands.cancel_batch_generation_task(
                    cancel_db,
                    task_id=task_id,
                    authorized_project_id="project",
                )
            raise RuntimeError("queue response lost")

    with (
        patch.object(batch_generation_commands, "get_task_queue", return_value=_CancelThenFailQueue()),
        session_factory() as db,
        pytest.raises(AppError, match="批量生成任务入队失败"),
    ):
        batch_generation_commands.create_admitted_batch_generation_task(
            db,
            project_id="project",
            outline_id="outline",
            actor_user_id="owner",
            runtime_provider="openai",
            params_json="{}",
            chapter_refs=[("chapter", 1)],
            request_id="request",
        )

    with session_factory() as db:
        task = db.execute(sa.select(BatchGenerationTask)).scalar_one()
        item = db.execute(sa.select(BatchGenerationTaskItem)).scalar_one()
        assert task.status == "canceled"
        assert task.cancel_requested is True
        assert item.status == "canceled"
    engine.dispose()


@pytest.mark.parametrize(
    ("action", "expected_events"),
    [
        ("retry_failed", ["step_requeued", "retry", "checkpoint", "paused"]),
        ("skip_failed", ["step_skipped", "resumed", "checkpoint", "paused"]),
    ],
)
def test_requeue_enqueue_failure_persists_action_checkpoint_event(
    tmp_path: Path,
    action: str,
    expected_events: list[str],
) -> None:
    engine, session_factory = _session_factory(tmp_path / f"{action}-event.db")
    with session_factory() as db:
        _seed_task(db, task_id="task", status="paused", failed_item=True, project_task=True)
        if action == "skip_failed":
            db.add(
                BatchGenerationTaskItem(
                    id="item-pending",
                    task_id="task",
                    chapter_id=None,
                    chapter_number=2,
                    status="queued",
                )
            )
            task = db.get(BatchGenerationTask, "task")
            assert task is not None
            task.total_count = 2
            db.commit()

    class _FailingQueue:
        def enqueue_batch_generation_task(self, _task_id: str) -> str:
            raise RuntimeError("queue down")

    with (
        patch.object(batch_generation_commands, "get_task_queue", return_value=_FailingQueue()),
        session_factory() as db,
        pytest.raises(AppError, match="批量生成任务入队失败"),
    ):
        _invoke(db, action=action, task_id="task")

    with session_factory() as db:
        task = db.get(BatchGenerationTask, "task")
        assert task is not None
        assert task.status == "paused"
        events = db.execute(
            sa.select(ProjectTaskEvent)
            .where(ProjectTaskEvent.task_id == "runtime-task")
            .order_by(ProjectTaskEvent.seq.asc())
        ).scalars().all()
        assert [event.event_type for event in events] == expected_events
        checkpoint = events[-2]
        payload = json.loads(str(checkpoint.payload_json or "{}"))
        assert payload["reason"] == f"{action}_enqueue_failed"
        assert payload["checkpoint"]["status"] == "queued"
        assert payload["error"]["code"] == "QUEUE_ENQUEUE_ERROR"
    engine.dispose()


def test_mutation_routes_only_authorize_serialize_and_delegate() -> None:
    tree = ast.parse(Path(batch_generation_routes.__file__).read_text(encoding="utf-8"))
    route_names = {
        "create_batch_generation_task",
        "pause_batch_generation_task",
        "resume_batch_generation_task",
        "retry_failed_batch_generation_task",
        "skip_failed_batch_generation_task",
        "cancel_batch_generation_task",
    }
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in route_names
    }
    assert set(functions) == route_names

    forbidden_calls = {
        "append_batch_project_task_event",
        "append_project_task_event",
        "finalize_batch_project_task",
        "pause_batch_generation",
        "recalculate_batch_generation_counts",
        "requeue_batch_project_task",
        "sync_batch_generation_checkpoint",
    }
    for name, function in functions.items():
        for node in ast.walk(function):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    assert not (
                        isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "db"
                        and node.func.attr == "commit"
                    ), name
                    called_name = node.func.attr
                elif isinstance(node.func, ast.Name):
                    called_name = node.func.id
                else:
                    called_name = ""
                assert called_name not in forbidden_calls, name
                assert called_name != "BatchGenerationTaskItem", name
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                assert all(
                    not (isinstance(target, ast.Attribute) and target.attr == "status")
                    for target in targets
                ), name

    forbidden_domain_names = {
        "BatchGenerationTask",
        "BatchGenerationTaskItem",
        "Chapter",
        "enter_batch_generation_quota_admission",
        "lock_and_enforce_batch_generation_quotas",
        "resolve_task_runtime_provider",
        "ensure_active_outline",
        "select",
    }
    assert not {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id in forbidden_domain_names
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        assert not (
            isinstance(node.func.value, ast.Name)
            and node.func.value.id == "db"
            and node.func.attr in {"execute", "get", "commit", "flush", "add", "add_all"}
        )
