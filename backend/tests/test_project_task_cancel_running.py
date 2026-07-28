"""backend-generation#9：ProjectTask 协作式取消（flag + 边界轮询）。

契约：queued 直接取消；running 只置 cancel_requested，worker 在下一个诚实
边界（派发前 / stale 重排队前）消费该标志收敛为 canceled；已完成的工作绝不
回滚。SSE 客户端断开必须留结构化 warning（不中断已启动的阻塞步骤）。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.project_settings import ProjectSettings
from app.models.project_task import ProjectTask
from app.models.project_task_event import ProjectTaskEvent
from app.services import project_task_service


def _factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _seed_task(db: Session, *, kind: str = "noop", status: str = "queued", cancel_requested: bool = False) -> None:
    db.add(
        ProjectTask(
            id="pt-1",
            project_id="p1",
            actor_user_id=None,
            kind=kind,
            status=status,
            idempotency_key=f"{kind}:1",
            cancel_requested=cancel_requested,
        )
    )
    db.commit()


def _events(db: Session) -> list[str]:
    rows = db.query(ProjectTaskEvent).filter(ProjectTaskEvent.task_id == "pt-1").all()
    return [str(row.event_type) for row in rows]


def test_cancel_running_task_sets_flag_and_keeps_running() -> None:
    factory = _factory()
    with factory() as db:
        _seed_task(db, status="running")
        task = db.get(ProjectTask, "pt-1")
        out = project_task_service.cancel_project_task(db=db, task=task)
        assert out.status == "running"
        assert out.cancel_requested is True

    with factory() as db:
        task = db.get(ProjectTask, "pt-1")
        assert task.status == "running"
        assert task.cancel_requested is True
        assert "cancel_requested" in _events(db)


def test_cancel_queued_task_still_cancels_immediately() -> None:
    factory = _factory()
    with factory() as db:
        _seed_task(db, status="queued")
        task = db.get(ProjectTask, "pt-1")
        out = project_task_service.cancel_project_task(db=db, task=task)
        assert out.status == "canceled"
        assert out.cancel_requested is True


def test_worker_finishes_preflagged_task_as_canceled_before_dispatch() -> None:
    factory = _factory()
    with factory() as db:
        _seed_task(db, kind="noop", status="queued", cancel_requested=True)

    with patch.object(project_task_service, "SessionLocal", factory):
        project_task_service.run_project_task(task_id="pt-1")

    with factory() as db:
        task = db.get(ProjectTask, "pt-1")
        assert task.status == "canceled"
        result = json.loads(task.result_json or "{}")
        assert result.get("canceled") is True
        assert task.finished_at is not None
        assert "canceled" in _events(db)


def test_vector_rebuild_stale_requeue_honors_cancel_request() -> None:
    factory = _factory()
    with factory() as db:
        _seed_task(db, kind="vector_rebuild", status="queued")
        db.add(ProjectSettings(project_id="p1", vector_dirty_revision=5))
        db.commit()

    def _fake_rebuild(**kwargs: Any) -> dict[str, Any]:
        # 模拟重建期间：用户请求取消 + 修订号推进（触发 stale 分支）。
        with factory() as db:
            task = db.get(ProjectTask, "pt-1")
            task.cancel_requested = True
            settings_row = db.get(ProjectSettings, "p1")
            settings_row.vector_dirty_revision = 6
            db.commit()
        return {"enabled": True, "skipped": False}

    with (
        patch.object(project_task_service, "SessionLocal", factory),
        patch("app.services.vector_rag_service.vector_rag_status", return_value={"enabled": True}),
        patch("app.services.vector_rag_service.rebuild_kb_vectors", side_effect=_fake_rebuild),
        patch("app.services.vector_kb_service.ensure_default_kb", return_value=None),
        patch("app.services.vector_embedding_overrides.vector_embedding_overrides", return_value={}),
    ):
        project_task_service.run_project_task(task_id="pt-1")

    with factory() as db:
        task = db.get(ProjectTask, "pt-1")
        assert task.status == "canceled", f"stale 重排队必须让位于取消请求, got {task.status}"
        result = json.loads(task.result_json or "{}")
        assert result.get("canceled") is True


def test_sse_client_disconnect_leaves_structured_warning() -> None:
    from app.services.chapter_generation import stream_service

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    with patch.object(stream_service, "log_event", lambda *args, **kwargs: calls.append((args, kwargs))):
        gen = stream_service.generate_chapter_stream_events(
            logger=stream_service.logging.getLogger("test"),
            request_id="rid-disconnect",
            request_path="/api/x",
            request_method="POST",
            prepared=None,  # type: ignore[arg-type]  # 首个事件产出前不会触碰
            chapter_id="ch-1",
            body=None,  # type: ignore[arg-type]
            user_id="u1",
        )
        next(gen)
        gen.close()

    disconnects = [fields for _, fields in calls if fields.get("event") == "SSE_CLIENT_DISCONNECTED"]
    assert len(disconnects) == 1
    assert disconnects[0]["request_id"] == "rid-disconnect"
    assert disconnects[0]["chapter_id"] == "ch-1"
