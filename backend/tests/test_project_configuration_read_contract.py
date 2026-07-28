from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import threading
from pathlib import Path
from typing import Generator

import pytest
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api.routes import export as export_routes
from app.api.routes import llm_preset as llm_preset_routes
from app.api.routes import llm_task_presets as llm_task_preset_routes
from app.api.routes import outline as outline_routes
from app.api.routes import prompt_studio as prompt_studio_routes
from app.api.routes import prompts as prompts_routes
from app.api.routes import writing_styles as writing_style_routes
from app.core.errors import AppError
from app.db.base import Base
from app.db.session import get_db
from app.main import app_error_handler, validation_error_handler
from app.models.project import Project
from app.models.project_membership import ProjectMembership
from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
from app.models.user import User
from app.services.prompt_preset_defaults import (
    get_active_preset_for_task,
    prompt_template_hash,
    sync_builtin_prompt_defaults,
)


def _make_app(session_factory: sessionmaker) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _identity(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = "rid-config-read"
        user_id = request.headers.get("X-Test-User")
        request.state.user_id = user_id
        request.state.authenticated_user_id = user_id
        request.state.session_expire_at = None
        request.state.auth_source = "test"
        return await call_next(request)

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    for router in (
        prompts_routes.router,
        prompt_studio_routes.router,
        llm_preset_routes.router,
        llm_task_preset_routes.router,
        writing_style_routes.router,
        export_routes.router,
        outline_routes.router,
    ):
        app.include_router(router, prefix="/api")

    def _db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    return app


def _snapshot_database(engine) -> dict[str, list[tuple[str, ...]]]:  # type: ignore[no-untyped-def]
    snapshot: dict[str, list[tuple[str, ...]]] = {}
    with engine.connect() as conn:
        for table_name in sorted(inspect(conn).get_table_names()):
            quoted = conn.dialect.identifier_preparer.quote(table_name)
            rows = conn.execute(text(f"SELECT * FROM {quoted}")).all()
            snapshot[table_name] = sorted(tuple(repr(value) for value in row) for row in rows)
    return snapshot


@pytest.fixture
def config_app():  # type: ignore[no-untyped-def]
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                User(id="u_owner", display_name="owner"),
                User(id="u_editor", display_name="editor"),
                User(id="u_viewer", display_name="viewer"),
                User(id="u_outsider", display_name="outsider"),
                Project(id="p1", owner_user_id="u_owner", name="Project 1", genre=None, logline=None),
                ProjectMembership(project_id="p1", user_id="u_owner", role="owner"),
                ProjectMembership(project_id="p1", user_id="u_editor", role="editor"),
                ProjectMembership(project_id="p1", user_id="u_viewer", role="viewer"),
            ]
        )
        db.commit()
        presets = sync_builtin_prompt_defaults(db, project_id="p1")
        plan = next(row for row in presets if row.resource_key == "plan_chapter_v1")
        plan_id = plan.id
    yield TestClient(_make_app(factory)), engine, factory, plan_id
    engine.dispose()


def test_project_configuration_gets_are_viewer_reads_without_database_changes(config_app) -> None:  # type: ignore[no-untyped-def]
    client, engine, _factory, plan_id = config_app
    endpoints = [
        "/api/projects/p1/prompt_presets",
        "/api/projects/p1/prompt_preset_resources",
        f"/api/prompt_presets/{plan_id}",
        f"/api/prompt_presets/{plan_id}/export",
        "/api/projects/p1/prompt_presets/export_all",
        "/api/projects/p1/prompt-studio/categories",
        f"/api/projects/p1/prompt-studio/presets/{plan_id}?category=plan_chapter",
        "/api/projects/p1/llm_preset",
        "/api/projects/p1/llm_task_presets",
        "/api/projects/p1/writing_style_default",
        "/api/projects/p1/export/bundle",
        "/api/projects/p1/outline",
    ]

    for endpoint in endpoints:
        before = _snapshot_database(engine)
        response = client.get(endpoint, headers={"X-Test-User": "u_viewer"})
        assert response.status_code == 200, (endpoint, response.text)
        assert _snapshot_database(engine) == before, endpoint


def test_project_configuration_reads_fail_closed_for_outsiders(config_app) -> None:  # type: ignore[no-untyped-def]
    client, _engine, _factory, plan_id = config_app
    endpoints = [
        "/api/projects/p1/prompt_presets",
        "/api/projects/p1/prompt_preset_resources",
        f"/api/prompt_presets/{plan_id}",
        f"/api/prompt_presets/{plan_id}/export",
        "/api/projects/p1/prompt_presets/export_all",
        "/api/projects/p1/prompt-studio/categories",
        f"/api/projects/p1/prompt-studio/presets/{plan_id}?category=plan_chapter",
        "/api/projects/p1/llm_preset",
        "/api/projects/p1/llm_task_presets",
        "/api/projects/p1/writing_style_default",
        "/api/projects/p1/export/bundle",
        "/api/projects/p1/outline",
    ]

    for endpoint in endpoints:
        response = client.get(endpoint, headers={"X-Test-User": "u_outsider"})
        assert response.status_code == 404, (endpoint, response.text)


@pytest.mark.parametrize(
    ("method", "endpoint", "payload"),
    [
        (
            "POST",
            "/api/projects/p1/prompt_presets",
            {"name": "Custom preset", "scope": "project", "version": 1},
        ),
        (
            "POST",
            "/api/projects/p1/prompt-studio/presets?category=plan_chapter",
            {"name": "Custom studio preset", "content": "content"},
        ),
        (
            "PUT",
            "/api/projects/p1/llm_preset",
            {"provider": "openai", "model": "gpt-4o-mini"},
        ),
        (
            "PUT",
            "/api/projects/p1/llm_task_presets/chapter_generate",
            {"provider": "openai", "model": "gpt-4o-mini"},
        ),
        (
            "PUT",
            "/api/projects/p1/writing_style_default",
            {"style_id": None},
        ),
    ],
)
def test_project_configuration_write_matrix_requires_editor(
    config_app,  # type: ignore[no-untyped-def]
    method: str,
    endpoint: str,
    payload: dict[str, object],
) -> None:
    client, _engine, _factory, _plan_id = config_app
    viewer = client.request(method, endpoint, headers={"X-Test-User": "u_viewer"}, json=payload)
    assert viewer.status_code == 403, (endpoint, viewer.text)
    outsider = client.request(method, endpoint, headers={"X-Test-User": "u_outsider"}, json=payload)
    assert outsider.status_code == 404, (endpoint, outsider.text)
    editor = client.request(method, endpoint, headers={"X-Test-User": "u_editor"}, json=payload)
    assert editor.status_code == 200, (endpoint, editor.text)


def test_builtin_sync_is_editor_only_idempotent_and_protects_custom_content(config_app) -> None:  # type: ignore[no-untyped-def]
    client, engine, factory, plan_id = config_app
    viewer = client.post(
        "/api/projects/p1/prompt_presets/sync_builtin_defaults",
        headers={"X-Test-User": "u_viewer"},
    )
    assert viewer.status_code == 403
    outsider = client.post(
        "/api/projects/p1/prompt_presets/sync_builtin_defaults",
        headers={"X-Test-User": "u_outsider"},
    )
    assert outsider.status_code == 404

    with factory() as db:
        preset = db.get(PromptPreset, plan_id)
        assert preset is not None
        block = (
            db.query(PromptBlock)
            .filter(PromptBlock.preset_id == plan_id)
            .order_by(PromptBlock.injection_order.asc())
            .first()
        )
        assert block is not None
        preset.version = 0
        original_hash = prompt_template_hash(block.template)
        block.origin_template_hash = original_hash
        block.resource_template_outdated = False
        block.template = "user-custom-template"
        db.commit()

    before_get = _snapshot_database(engine)
    read = client.get("/api/projects/p1/prompt_presets", headers={"X-Test-User": "u_viewer"})
    assert read.status_code == 200
    assert _snapshot_database(engine) == before_get
    with factory() as db:
        assert db.get(PromptPreset, plan_id).version == 0  # type: ignore[union-attr]

    synced = client.post(
        "/api/projects/p1/prompt_presets/sync_builtin_defaults",
        headers={"X-Test-User": "u_editor"},
    )
    assert synced.status_code == 200
    assert synced.json()["data"]["synced"] == 6
    with factory() as db:
        preset = db.get(PromptPreset, plan_id)
        assert preset is not None and preset.version > 0
        block = db.query(PromptBlock).filter(PromptBlock.preset_id == plan_id).order_by(PromptBlock.injection_order.asc()).first()
        assert block is not None
        assert block.template == "user-custom-template"
        assert block.resource_template_outdated is True

    after_first_sync = _snapshot_database(engine)
    second = client.post(
        "/api/projects/p1/prompt_presets/sync_builtin_defaults",
        headers={"X-Test-User": "u_editor"},
    )
    assert second.status_code == 200
    assert _snapshot_database(engine) == after_first_sync


def test_builtin_sync_rolls_back_the_whole_baseline_on_resource_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add(User(id="u1", display_name="user"))
        db.add(Project(id="p1", owner_user_id="u1", name="Project", genre=None, logline=None))
        db.commit()

    defaults = importlib.import_module("app.services.prompt_preset_defaults")
    real_loader = defaults.load_preset_resource

    def _failing_loader(resource_key: str):  # type: ignore[no-untyped-def]
        if resource_key == "post_edit_v1":
            raise RuntimeError("injected resource failure")
        return real_loader(resource_key)

    monkeypatch.setattr(defaults, "load_preset_resource", _failing_loader)
    with factory() as db, pytest.raises(RuntimeError, match="injected resource failure"):
        sync_builtin_prompt_defaults(db, project_id="p1")
    with factory() as db:
        assert db.query(PromptPreset).count() == 0
        assert db.query(PromptBlock).count() == 0
    engine.dispose()


def test_empty_prompt_list_and_studio_gets_do_not_initialize_any_rows() -> None:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                User(id="u1", display_name="viewer"),
                Project(id="p1", owner_user_id="u1", name="Project", genre=None, logline=None),
                ProjectMembership(project_id="p1", user_id="u1", role="viewer"),
            ]
        )
        db.commit()
    client = TestClient(_make_app(factory))
    before = _snapshot_database(engine)

    list_response = client.get("/api/projects/p1/prompt_presets", headers={"X-Test-User": "u1"})
    studio_response = client.get("/api/projects/p1/prompt-studio/categories", headers={"X-Test-User": "u1"})

    assert list_response.status_code == 200
    assert list_response.json()["data"]["presets"] == []
    assert studio_response.status_code == 200
    prompt_categories = studio_response.json()["data"]["categories"][:-1]
    assert prompt_categories and all(category["presets"] == [] for category in prompt_categories)
    assert _snapshot_database(engine) == before
    with factory() as db, pytest.raises(AppError, match="synchronize defaults"):
        get_active_preset_for_task(
            db,
            project_id="p1",
            task="plan_chapter",
            allow_autocreate=True,
        )
    assert _snapshot_database(engine) == before
    engine.dispose()


def test_builtin_sync_is_safe_under_sqlite_concurrency(tmp_path: Path) -> None:
    db_path = tmp_path / "prompt-sync.sqlite3"
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add(User(id="u1", display_name="user"))
        db.add(Project(id="p1", owner_user_id="u1", name="Project", genre=None, logline=None))
        db.commit()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            with factory() as db:
                barrier.wait(timeout=5)
                sync_builtin_prompt_defaults(db, project_id="p1")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=_run), threading.Thread(target=_run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors
    with factory() as db:
        presets = db.query(PromptPreset).filter(PromptPreset.project_id == "p1").all()
        assert len(presets) == 6
        assert len({preset.resource_key for preset in presets}) == 6
        for preset in presets:
            blocks = db.query(PromptBlock).filter(PromptBlock.preset_id == preset.id).all()
            assert len({block.identifier for block in blocks}) == len(blocks)
    engine.dispose()


def test_uniqueness_migration_fails_closed_when_existing_rows_are_duplicated(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE prompt_presets (project_id TEXT, resource_key TEXT)")
        conn.exec_driver_sql("INSERT INTO prompt_presets VALUES ('p1', 'plan_chapter_v1')")
        conn.exec_driver_sql("INSERT INTO prompt_presets VALUES ('p1', 'plan_chapter_v1')")

        migration_path = (
            Path(__file__).parents[1]
            / "alembic"
            / "versions"
            / "d8f3b5a7c9e1_unique_builtin_prompt_resources.py"
        )
        spec = importlib.util.spec_from_file_location("prompt_unique_migration", migration_path)
        assert spec is not None and spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)
        with pytest.raises(RuntimeError, match="without discarding user content"):
            migration._require_no_duplicates()
    engine.dispose()


def test_get_handlers_do_not_call_mutating_ensurers_or_session_writes() -> None:
    route_paths = [
        Path(prompts_routes.__file__),
        Path(prompt_studio_routes.__file__),
        Path(llm_preset_routes.__file__),
        Path(llm_task_preset_routes.__file__),
        Path(writing_style_routes.__file__),
        Path(export_routes.__file__),
        Path(outline_routes.__file__),
    ]
    forbidden_calls: list[str] = []
    for route_path in route_paths:
        tree = ast.parse(route_path.read_text(encoding="utf-8"), filename=str(route_path))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_get = any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "get"
                for decorator in node.decorator_list
            )
            if not is_get:
                continue
            for call in (item for item in ast.walk(node) if isinstance(item, ast.Call)):
                name = ""
                if isinstance(call.func, ast.Name):
                    name = call.func.id
                elif isinstance(call.func, ast.Attribute):
                    name = call.func.attr
                if name in {"add", "add_all", "commit", "delete", "flush", "rollback"} or name.startswith(
                    ("ensure_", "sync_")
                ):
                    forbidden_calls.append(f"{route_path.name}:{node.name}:{name}")
    assert forbidden_calls == []
