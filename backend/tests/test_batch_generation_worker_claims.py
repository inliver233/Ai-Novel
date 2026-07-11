from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.models.batch_generation_task import BatchGenerationTask, BatchGenerationTaskItem
from app.models.chapter import Chapter
from app.services import batch_generation_commands, batch_generation_service
from app.services.generation_service import PreparedLlmCall
from tests.support import create_tables


def _factory(path: Path) -> tuple[sa.Engine, sessionmaker[Session]]:
    engine = sa.create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    create_tables(engine)
    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _seed(factory: sessionmaker[Session], *, with_chapter: bool = False) -> None:
    with factory() as db:
        db.add(
            BatchGenerationTask(
                id="task",
                project_id="project",
                outline_id="outline",
                actor_user_id="owner",
                runtime_provider="openai",
                project_task_id=None,
                status="queued",
                total_count=1,
                params_json=json.dumps({"context": {}}, ensure_ascii=False),
            )
        )
        db.add(
            BatchGenerationTaskItem(
                id="item",
                task_id="task",
                chapter_id="chapter" if with_chapter else None,
                chapter_number=1,
                status="queued",
            )
        )
        if with_chapter:
            db.add(
                Chapter(
                    id="chapter",
                    project_id="project",
                    outline_id="outline",
                    number=1,
                    title="Chapter",
                    plan="Plan",
                )
            )
        db.commit()


def _race(workers):  # type: ignore[no-untyped-def]
    barrier = threading.Barrier(2)
    results: list[bool] = []
    errors: list[BaseException] = []

    def _run(worker):  # type: ignore[no-untyped-def]
        try:
            barrier.wait(timeout=10)
            results.append(bool(worker()))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=_run, args=(worker,)) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    return results


def test_sqlite_task_and_item_claims_have_one_winner(tmp_path: Path) -> None:
    engine, factory = _factory(tmp_path / "claims.db")
    _seed(factory)

    def _claim_task() -> bool:
        with factory() as db:
            claimed = batch_generation_commands.claim_batch_generation_task_for_worker(db, task_id="task")
            db.commit()
            return claimed

    task_results = _race([_claim_task, _claim_task])
    assert sorted(task_results) == [False, True]

    def _claim_item(request_id: str):
        def _claim() -> bool:
            with factory() as db:
                return batch_generation_commands.claim_batch_generation_item_for_worker(
                    db,
                    task_id="task",
                    item_id="item",
                    request_id=request_id,
                )

        return _claim

    item_results = _race([_claim_item("request-1"), _claim_item("request-2")])
    assert sorted(item_results) == [False, True]
    with factory() as db:
        item = db.get(BatchGenerationTaskItem, "item")
        assert item is not None
        assert item.status == "running"
        assert item.attempt_count == 1
        assert item.last_request_id in {"request-1", "request-2"}
    engine.dispose()


def test_worker_claims_fail_closed_on_control_flags(tmp_path: Path) -> None:
    engine, factory = _factory(tmp_path / "claim-flags.db")
    _seed(factory)
    with factory() as db:
        task = db.get(BatchGenerationTask, "task")
        assert task is not None
        task.cancel_requested = True
        db.commit()
    with factory() as db:
        assert batch_generation_commands.claim_batch_generation_task_for_worker(db, task_id="task") is False
        db.rollback()
    with factory() as db:
        task = db.get(BatchGenerationTask, "task")
        assert task is not None
        task.cancel_requested = False
        db.commit()
        assert batch_generation_commands.claim_batch_generation_task_for_worker(db, task_id="task") is True
        db.commit()
    with factory() as db:
        task = db.get(BatchGenerationTask, "task")
        assert task is not None
        task.pause_requested = True
        db.commit()
        assert (
            batch_generation_commands.claim_batch_generation_item_for_worker(
                db,
                task_id="task",
                item_id="item",
                request_id="blocked",
            )
            is False
        )
    with factory() as db:
        item = db.get(BatchGenerationTaskItem, "item")
        assert item is not None
        assert item.status == "queued"
        assert item.attempt_count == 0
    engine.dispose()


def test_duplicate_worker_delivery_invokes_external_generation_once(tmp_path: Path) -> None:
    engine, factory = _factory(tmp_path / "workers.db")
    _seed(factory, with_chapter=True)
    prepare_entered = threading.Event()
    release_prepare = threading.Event()
    errors: list[BaseException] = []
    llm_calls: list[str] = []
    llm_call = PreparedLlmCall(
        provider="openai",
        model="test",
        base_url="https://llm.invalid",
        timeout_seconds=10,
        params={},
        params_json="{}",
        extra={},
    )

    def _prepare(**_kwargs):  # type: ignore[no-untyped-def]
        prepare_entered.set()
        assert release_prepare.wait(timeout=15)
        return (SimpleNamespace(id="project"), llm_call, "key", "", "", "", "", "", {})

    def _generate(**_kwargs):  # type: ignore[no-untyped-def]
        llm_calls.append("called")
        return SimpleNamespace(data={"content_md": "Generated", "summary": "Summary"}, run_id="run-1")

    def _worker() -> None:
        try:
            batch_generation_service.run_batch_generation_task(task_id="task")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    with (
        patch.object(batch_generation_service, "SessionLocal", factory),
        patch.object(batch_generation_service, "_prepare_project_context", side_effect=_prepare),
        patch.object(batch_generation_service, "assemble_chapter_generate_render_values", return_value=({}, {})),
        patch.object(
            batch_generation_service,
            "render_preset_for_task",
            return_value=("", "", [], None, None, None, {}),
        ),
        patch.object(batch_generation_service, "run_chapter_generate_llm_step", side_effect=_generate),
    ):
        first = threading.Thread(target=_worker)
        first.start()
        assert prepare_entered.wait(timeout=15)
        second = threading.Thread(target=_worker)
        second.start()
        second.join(timeout=15)
        release_prepare.set()
        first.join(timeout=20)

    assert not errors
    assert not first.is_alive() and not second.is_alive()
    assert llm_calls == ["called"]
    with factory() as db:
        task = db.get(BatchGenerationTask, "task")
        item = db.get(BatchGenerationTaskItem, "item")
        assert task is not None and item is not None
        assert task.status == "succeeded"
        assert item.status == "succeeded"
        assert item.attempt_count == 1
    engine.dispose()


def test_worker_initialization_error_rolls_claim_back_to_queued(tmp_path: Path) -> None:
    engine, factory = _factory(tmp_path / "initialization-rollback.db")
    _seed(factory, with_chapter=True)

    with (
        patch.object(batch_generation_service, "SessionLocal", factory),
        patch.object(batch_generation_service, "_parse_params", side_effect=RuntimeError("invalid params")),
    ):
        try:
            batch_generation_service.run_batch_generation_task(task_id="task")
        except RuntimeError as exc:
            assert str(exc) == "invalid params"
        else:  # pragma: no cover - defensive
            raise AssertionError("initialization error was not surfaced")

    with factory() as db:
        task = db.get(BatchGenerationTask, "task")
        assert task is not None
        assert task.status == "queued"
    engine.dispose()


def test_worker_does_not_reclaim_preexisting_running_item(tmp_path: Path) -> None:
    engine, factory = _factory(tmp_path / "running-item.db")
    _seed(factory, with_chapter=True)
    with factory() as db:
        item = db.get(BatchGenerationTaskItem, "item")
        assert item is not None
        item.status = "running"
        item.attempt_count = 4
        db.commit()

    llm_call = PreparedLlmCall(
        provider="openai",
        model="test",
        base_url="https://llm.invalid",
        timeout_seconds=10,
        params={},
        params_json="{}",
        extra={},
    )

    with (
        patch.object(batch_generation_service, "SessionLocal", factory),
        patch.object(
            batch_generation_service,
            "_prepare_project_context",
            return_value=(SimpleNamespace(id="project"), llm_call, "key", "", "", "", "", "", {}),
        ) as prepare,
        patch.object(batch_generation_service, "run_chapter_generate_llm_step") as llm_step,
    ):
        batch_generation_service.run_batch_generation_task(task_id="task")

    prepare.assert_called_once()
    llm_step.assert_not_called()
    with factory() as db:
        item = db.get(BatchGenerationTaskItem, "item")
        assert item is not None
        assert item.status == "running"
        assert item.attempt_count == 4
    engine.dispose()
