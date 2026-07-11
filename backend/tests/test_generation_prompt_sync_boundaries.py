from __future__ import annotations

import ast
import logging
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.errors import AppError
from app.models.project import Project
from app.models.outline import Outline
from app.models.chapter import Chapter
from app.models.batch_generation_task import BatchGenerationTask
from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
from app.services.generation_pipeline import run_content_optimize_step, run_post_edit_step
from app.services.generation_service import PreparedLlmCall
from app.services.prompt_presets import get_active_preset_for_task
from app.services.chapter_generation.app_service import generate_chapter, plan_chapter
from app.schemas.chapter_plan import ChapterPlanRequest
from app.schemas.chapter_generate import ChapterGenerateRequest
from tests.test_batch_generation_worker_claims import _factory as batch_factory, _seed as seed_batch
from tests.support import create_tables, make_session_factory, make_sqlite_engine, seed_user


PROJECT_ID = "p1"
USER_ID = "u1"


@pytest.fixture
def empty_prompt_env():
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine)
    seed_user(factory, user_id=USER_ID)
    with factory() as db:
        db.add(Project(id=PROJECT_ID, owner_user_id=USER_ID, name="Empty prompts"))
        db.commit()
    yield factory
    engine.dispose()


@pytest.mark.parametrize("task", ("post_edit", "content_optimize"))
def test_rewrite_generation_without_explicit_sync_is_read_only_and_never_calls_llm(empty_prompt_env, task: str) -> None:
    factory = empty_prompt_env
    call = PreparedLlmCall(
        provider="openai",
        model="test",
        base_url="https://llm.invalid",
        timeout_seconds=10,
        params={},
        params_json="{}",
        extra={},
    )
    runner = run_post_edit_step if task == "post_edit" else run_content_optimize_step
    with factory() as db:
        before = (db.query(PromptPreset).count(), db.query(PromptBlock).count())
    with (
        patch("app.services.generation_pipeline.SessionLocal", factory),
        patch("app.services.generation_pipeline.call_llm_and_record") as llm,
        pytest.raises(AppError, match="synchronize defaults or activate one"),
    ):
        runner(
            logger=logging.getLogger("test"),
            request_id="rid",
            actor_user_id=USER_ID,
            project_id=PROJECT_ID,
            chapter_id=None,
            api_key="key",
            llm_call=call,
            render_values={},
            raw_content="content",
            macro_seed="seed",
        )
    llm.assert_not_called()
    with factory() as db:
        assert (db.query(PromptPreset).count(), db.query(PromptBlock).count()) == before


def test_concurrent_empty_prompt_lookup_fails_stably_without_writes(empty_prompt_env) -> None:
    factory = empty_prompt_env
    barrier = threading.Barrier(2)
    errors: list[str] = []

    def _lookup() -> None:
        with factory() as db:
            barrier.wait(timeout=10)
            try:
                get_active_preset_for_task(db, project_id=PROJECT_ID, task="plan_chapter")
            except AppError as exc:
                errors.append(exc.message)

    threads = [threading.Thread(target=_lookup), threading.Thread(target=_lookup)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == [
        "No PromptPreset is configured for task=plan_chapter; synchronize defaults or activate one in Prompt Studio first",
        "No PromptPreset is configured for task=plan_chapter; synchronize defaults or activate one in Prompt Studio first",
    ]
    with factory() as db:
        assert db.query(PromptPreset).count() == 0
        assert db.query(PromptBlock).count() == 0


def test_generation_modules_do_not_import_or_call_default_prompt_ensurers() -> None:
    backend = Path(__file__).resolve().parents[1]
    paths = (
        backend / "app/services/generation_pipeline.py",
        backend / "app/services/chapter_generation/plan_prepare_service.py",
        backend / "app/services/chapter_generation/prepare_service.py",
        backend / "app/services/batch_generation_service.py",
    )
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id.startswith("ensure_default")
        }
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name.startswith("ensure_default")
        }
        assert names == set(), path
        assert imported == set(), path


def _seed_chapter(factory) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        project = db.get(Project, PROJECT_ID)
        if project is None:
            db.add(Project(id=PROJECT_ID, owner_user_id=USER_ID, name="Project", active_outline_id="o1"))
        db.add(Outline(id="o1", project_id=PROJECT_ID, title="Outline", content_md="Outline"))
        db.add(Chapter(id="c1", project_id=PROJECT_ID, outline_id="o1", number=1, title="Chapter", plan="Plan"))
        db.commit()


def _prompt_snapshot(factory):  # type: ignore[no-untyped-def]
    with factory() as db:
        return (
            [tuple(row) for row in db.execute(PromptPreset.__table__.select().order_by(PromptPreset.id)).all()],
            [tuple(row) for row in db.execute(PromptBlock.__table__.select().order_by(PromptBlock.id)).all()],
        )


def _resolved_llm() -> SimpleNamespace:
    return SimpleNamespace(
        api_key="key",
        llm_call=PreparedLlmCall(
            provider="openai",
            model="test",
            base_url="https://llm.invalid",
            timeout_seconds=10,
            params={},
            params_json="{}",
            extra={},
        ),
    )


def test_chapter_plan_prepare_empty_prompt_is_read_only_and_calls_no_llm(empty_prompt_env) -> None:
    factory = empty_prompt_env
    _seed_chapter(factory)
    before = _prompt_snapshot(factory)
    with (
        patch("app.services.chapter_generation.plan_prepare_service.SessionLocal", factory),
        patch(
            "app.services.chapter_generation.plan_prepare_service.resolve_task_llm_for_call",
            return_value=_resolved_llm(),
        ),
        patch("app.services.chapter_generation.app_service.run_plan_llm_step") as plan_llm,
        pytest.raises(AppError, match="synchronize defaults or activate one"),
    ):
        plan_chapter(
            logger=logging.getLogger(__name__),
            request_id="rid",
            chapter_id="c1",
            body=ChapterPlanRequest(instruction="plan"),
            user_id=USER_ID,
            x_llm_provider=None,
            x_llm_api_key=None,
        )
    plan_llm.assert_not_called()
    assert _prompt_snapshot(factory) == before


def test_chapter_generate_plan_first_empty_prompt_is_read_only_and_calls_no_llm(empty_prompt_env) -> None:
    factory = empty_prompt_env
    _seed_chapter(factory)
    before = _prompt_snapshot(factory)
    with (
        patch("app.services.chapter_generation.prepare_service.SessionLocal", factory),
        patch(
            "app.services.chapter_generation.prepare_service.resolve_task_llm_for_call", return_value=_resolved_llm()
        ),
        patch("app.services.chapter_generation.app_service.run_plan_llm_step") as plan_llm,
        patch("app.services.chapter_generation.app_service.run_chapter_generate_llm_step") as chapter_llm,
        pytest.raises(AppError, match="synchronize defaults or activate one"),
    ):
        generate_chapter(
            logger=logging.getLogger(__name__),
            request_id="rid",
            chapter_id="c1",
            body=ChapterGenerateRequest(mode="replace", instruction="draft", plan_first=True),
            user_id=USER_ID,
            x_llm_provider=None,
            x_llm_api_key=None,
        )
    plan_llm.assert_not_called()
    chapter_llm.assert_not_called()
    assert _prompt_snapshot(factory) == before


def test_batch_plan_first_empty_prompt_fails_without_llm_or_prompt_writes(tmp_path: Path) -> None:
    engine, factory = batch_factory(tmp_path / "batch-empty-prompts.db")
    seed_batch(factory, with_chapter=True)
    with factory() as db:
        task = db.get(BatchGenerationTask, "task")
        assert task is not None
        task.params_json = '{"context": {}, "plan_first": true}'
        db.commit()
    before = _prompt_snapshot(factory)
    with (
        patch("app.services.batch_generation_service.SessionLocal", factory),
        patch(
            "app.services.batch_generation_service._prepare_project_context",
            return_value=(
                SimpleNamespace(id="project", name="Project", genre=None, logline=None),
                _resolved_llm().llm_call,
                "key",
                "",
                "",
                "",
                "",
                "",
                {},
            ),
        ),
        patch("app.services.batch_generation_service.run_plan_llm_step") as plan_llm,
        patch("app.services.batch_generation_service.run_chapter_generate_llm_step") as chapter_llm,
    ):
        from app.services.batch_generation_service import run_batch_generation_task

        run_batch_generation_task(task_id="task")
    plan_llm.assert_not_called()
    chapter_llm.assert_not_called()
    assert _prompt_snapshot(factory) == before
    with factory() as db:
        task = db.get(BatchGenerationTask, "task")
        assert task is not None
        assert task.status == "paused"
        assert "No PromptPreset is configured" in str(task.error_json)
    engine.dispose()
