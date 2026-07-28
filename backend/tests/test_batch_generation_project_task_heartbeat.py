"""backend-generation#10 残留：批任务心跳必须同步续约编排 ProjectTask 租约。

Wave1 的 batch 心跳线程只续 BatchGenerationTask.heartbeat_at，链接的
batch_generation_orchestrator ProjectTask 在长 LLM 调用期间仍会被
watchdog（120s）误判 failed。心跳一次续两租约（同一事务），且只在双方
均为 running 时生效。
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.utils import utc_now
from app.models.batch_generation_task import BatchGenerationTask
from app.models.project_task import ProjectTask
from app.services.batch_generation_service import touch_batch_generation_heartbeat


def _factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _seed(db: Session, *, batch_status: str = "running", project_task_status: str = "running") -> None:
    stale = utc_now() - timedelta(minutes=10)
    db.add(
        ProjectTask(
            id="pt-1",
            project_id="project",
            actor_user_id="owner",
            kind="batch_generation_orchestrator",
            status=project_task_status,
            idempotency_key="batch_generation:task",
            heartbeat_at=stale,
            attempt=1,
        )
    )
    db.add(
        BatchGenerationTask(
            id="task",
            project_id="project",
            outline_id="outline",
            actor_user_id="owner",
            project_task_id="pt-1",
            status=batch_status,
            heartbeat_at=stale,
        )
    )
    db.commit()


def test_touch_renews_both_batch_and_project_task_leases() -> None:
    factory = _factory()
    with factory() as db:
        _seed(db)
        stale_iso = db.get(ProjectTask, "pt-1").heartbeat_at.isoformat()

    with patch("app.services.batch_generation_service.SessionLocal", factory):
        assert touch_batch_generation_heartbeat(task_id="task") is True

    with factory() as db:
        batch = db.get(BatchGenerationTask, "task")
        project_task = db.get(ProjectTask, "pt-1")
        assert batch.heartbeat_at.isoformat() != stale_iso
        assert project_task.heartbeat_at.isoformat() != stale_iso


def test_touch_never_renews_non_running_project_task() -> None:
    factory = _factory()
    with factory() as db:
        _seed(db, project_task_status="paused")
        stale_iso = db.get(ProjectTask, "pt-1").heartbeat_at.isoformat()

    with patch("app.services.batch_generation_service.SessionLocal", factory):
        assert touch_batch_generation_heartbeat(task_id="task") is True

    with factory() as db:
        assert db.get(ProjectTask, "pt-1").heartbeat_at.isoformat() == stale_iso


def test_fresh_heartbeats_keep_watchdog_quiet() -> None:
    factory = _factory()
    with factory() as db:
        _seed(db)

    with patch("app.services.batch_generation_service.SessionLocal", factory):
        assert touch_batch_generation_heartbeat(task_id="task") is True

    from app.services.project_task_runtime_service import reconcile_project_tasks_once

    with patch("app.services.project_task_runtime_service.SessionLocal", factory):
        summary = reconcile_project_tasks_once(reason="test")

    assert summary["timed_out_running"] == 0
    assert summary["stale_batch_generation"] == 0
    with factory() as db:
        assert db.get(ProjectTask, "pt-1").status == "running"
        assert db.get(BatchGenerationTask, "task").status == "running"
