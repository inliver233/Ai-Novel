"""backend-api#6 残留：batch worker 崩溃后 stale-running 租约恢复。

契约：worker 认领时盖心跳租约、运行期后台心跳续约；watchdog 发现心跳超时的
running 批任务后，将 running item 置 failed 并把任务恢复为 paused（保留
resume/retry_failed/skip_failed/cancel 出路）。恢复必须锁内复检心跳，
活 worker（新鲜心跳）绝不能被误回收，恢复后的任务对旧 worker 的 item
认领关门（不双跑）。
"""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.utils import utc_now
from app.models.batch_generation_task import BatchGenerationTask, BatchGenerationTaskItem
from app.services import batch_generation_commands


def _factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _seed_running_task(
    db: Session,
    *,
    task_id: str = "task",
    heartbeat_age_seconds: int,
    status: str = "running",
) -> None:
    now = utc_now()
    db.add(
        BatchGenerationTask(
            id=task_id,
            project_id="project",
            outline_id="outline",
            actor_user_id="owner",
            status=status,
            total_count=2,
            heartbeat_at=now - timedelta(seconds=heartbeat_age_seconds),
        )
    )
    db.add(
        BatchGenerationTaskItem(
            id=f"item-{task_id}-1",
            task_id=task_id,
            chapter_id=None,
            chapter_number=1,
            status="running",
            started_at=now - timedelta(seconds=heartbeat_age_seconds),
        )
    )
    db.add(
        BatchGenerationTaskItem(
            id=f"item-{task_id}-2",
            task_id=task_id,
            chapter_id=None,
            chapter_number=2,
            status="queued",
        )
    )
    db.commit()


def test_stale_running_task_recovers_to_paused_with_failed_items() -> None:
    factory = _factory()
    with factory() as db:
        _seed_running_task(db, heartbeat_age_seconds=600)

    with factory() as db:
        recovered = batch_generation_commands.recover_stale_running_batch_generation_tasks(
            db, timeout_seconds=120
        )
    assert recovered == 1

    with factory() as db:
        task = db.get(BatchGenerationTask, "task")
        assert task is not None
        assert task.status == "paused"
        assert task.pause_requested is True
        error = json.loads(task.error_json or "{}")
        assert error.get("code") == "BATCH_GENERATION_HEARTBEAT_TIMEOUT"

        running_item = db.get(BatchGenerationTaskItem, "item-task-1")
        assert running_item is not None
        assert running_item.status == "failed"
        assert running_item.finished_at is not None
        item_error = json.loads(running_item.last_error_json or "{}")
        assert item_error.get("code") == "BATCH_GENERATION_HEARTBEAT_TIMEOUT"

        queued_item = db.get(BatchGenerationTaskItem, "item-task-2")
        assert queued_item is not None
        assert queued_item.status == "queued"


def test_fresh_running_task_is_never_recovered() -> None:
    factory = _factory()
    with factory() as db:
        _seed_running_task(db, heartbeat_age_seconds=5)

    with factory() as db:
        recovered = batch_generation_commands.recover_stale_running_batch_generation_tasks(
            db, timeout_seconds=120
        )
    assert recovered == 0

    with factory() as db:
        task = db.get(BatchGenerationTask, "task")
        assert task is not None
        assert task.status == "running"


def test_non_running_states_are_untouched_even_with_stale_heartbeat() -> None:
    factory = _factory()
    with factory() as db:
        _seed_running_task(db, task_id="paused-task", heartbeat_age_seconds=600, status="paused")
        _seed_running_task(db, task_id="queued-task", heartbeat_age_seconds=600, status="queued")

    with factory() as db:
        recovered = batch_generation_commands.recover_stale_running_batch_generation_tasks(
            db, timeout_seconds=120
        )
    assert recovered == 0

    with factory() as db:
        assert db.get(BatchGenerationTask, "paused-task").status == "paused"
        assert db.get(BatchGenerationTask, "queued-task").status == "queued"


def test_recovered_task_rejects_stale_worker_item_claims() -> None:
    """恢复后旧 worker 的 item 认领必须失败（status 已非 running，不双跑）。"""
    factory = _factory()
    with factory() as db:
        _seed_running_task(db, heartbeat_age_seconds=600)

    with factory() as db:
        assert (
            batch_generation_commands.recover_stale_running_batch_generation_tasks(db, timeout_seconds=120)
            == 1
        )

    with factory() as db:
        claimed = batch_generation_commands.claim_batch_generation_item_for_worker(
            db,
            task_id="task",
            item_id="item-task-2",
            request_id="stale-worker-retry",
        )
        assert claimed is False


def test_task_claim_stamps_heartbeat_lease() -> None:
    factory = _factory()
    with factory() as db:
        _seed_running_task(db, heartbeat_age_seconds=0, status="queued")
        task = db.get(BatchGenerationTask, "task")
        task.heartbeat_at = None
        db.commit()

    with factory() as db:
        assert batch_generation_commands.claim_batch_generation_task_for_worker(db, task_id="task")
        db.commit()

    with factory() as db:
        task = db.get(BatchGenerationTask, "task")
        assert task is not None
        assert task.status == "running"
        assert task.heartbeat_at is not None


def test_reconcile_loop_recovers_stale_batch_tasks() -> None:
    from unittest.mock import patch

    from app.services.project_task_runtime_service import reconcile_project_tasks_once

    factory = _factory()
    with factory() as db:
        _seed_running_task(db, heartbeat_age_seconds=600)

    with patch("app.services.project_task_runtime_service.SessionLocal", factory):
        summary = reconcile_project_tasks_once(reason="test")

    assert summary.get("stale_batch_generation") == 1
    with factory() as db:
        task = db.get(BatchGenerationTask, "task")
        assert task is not None
        assert task.status == "paused"
