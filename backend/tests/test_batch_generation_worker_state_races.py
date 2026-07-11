from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.models.batch_generation_task import BatchGenerationTask, BatchGenerationTaskItem
from app.services import batch_generation_commands, batch_generation_service


def _session_factory(database_path: Path) -> tuple[sa.Engine, sessionmaker[Session]]:
    engine = sa.create_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.mark.parametrize("action", ["cancel", "pause"])
def test_sqlite_worker_honors_control_race_before_step(
    tmp_path: Path,
    action: str,
) -> None:
    engine, session_factory = _session_factory(tmp_path / f"worker-{action}.db")
    with session_factory() as db:
        db.add(
            BatchGenerationTask(
                id="task",
                project_id="project",
                outline_id="outline",
                actor_user_id="owner",
                runtime_provider="openai",
                status="queued",
                total_count=1,
                params_json=json.dumps({"context": {}}, ensure_ascii=False),
            )
        )
        db.add(
            BatchGenerationTaskItem(
                id="item",
                task_id="task",
                chapter_id=None,
                chapter_number=1,
                status="queued",
            )
        )
        db.commit()

    prepare_entered = threading.Event()
    release_prepare = threading.Event()
    errors: list[BaseException] = []

    def _blocked_prepare(**_kwargs):  # type: ignore[no-untyped-def]
        prepare_entered.set()
        if not release_prepare.wait(timeout=15):
            raise RuntimeError("worker race test timed out")
        return (SimpleNamespace(id="project"), SimpleNamespace(provider="openai"), "key", "", "", "", "", "", {})

    def _run_worker() -> None:
        try:
            batch_generation_service.run_batch_generation_task(task_id="task")
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
            if action == "cancel":
                result = batch_generation_commands.cancel_batch_generation_task(
                    db,
                    task_id="task",
                    authorized_project_id="project",
                )
            else:
                result = batch_generation_commands.pause_batch_generation_task(
                    db,
                    task_id="task",
                    authorized_project_id="project",
                )
            assert result.applied is True
        release_prepare.set()
        worker.join(timeout=30)

    assert not errors
    assert not worker.is_alive()
    with session_factory() as db:
        task = db.get(BatchGenerationTask, "task")
        item = db.get(BatchGenerationTaskItem, "item")
        assert task is not None
        assert item is not None
        assert task.status == {"cancel": "canceled", "pause": "paused"}[action]
        assert item.status == {"cancel": "canceled", "pause": "queued"}[action]
    engine.dispose()
