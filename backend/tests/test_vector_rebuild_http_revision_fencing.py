from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from app.api.routes import vector as vector_routes
from app.db.base import Base
from app.db.session import get_db
from app.models.knowledge_base import KnowledgeBase
from app.models.project import Project
from app.models.project_settings import ProjectSettings
from app.models.user import User
from app.services.vector_index_state import mark_vector_index_dirty


PROJECT_ID = "project-vector-rebuild"
USER_ID = "user-vector-rebuild"


def _make_app(factory: sessionmaker[Session]) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _test_user(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = "request-vector-rebuild"
        request.state.user_id = USER_ID
        return await call_next(request)

    app.include_router(vector_routes.router, prefix="/api")

    def _override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = _override_get_db
    return app


@pytest.fixture
def session_factory(tmp_path) -> Generator[sessionmaker[Session], None, None]:  # type: ignore[no-untyped-def]
    engine = create_engine(
        f"sqlite:///{tmp_path / 'vector-rebuild-http.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            Project.__table__,
            ProjectSettings.__table__,
            KnowledgeBase.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    old_build_at = datetime(2025, 1, 2, 3, 4, tzinfo=timezone.utc)
    with factory() as db:
        db.add(User(id=USER_ID, display_name="Vector owner"))
        db.add(Project(id=PROJECT_ID, owner_user_id=USER_ID, name="Vector project"))
        db.add(
            ProjectSettings(
                project_id=PROJECT_ID,
                vector_index_dirty=True,
                vector_dirty_revision=7,
                last_vector_build_at=old_build_at,
            )
        )
        db.commit()
    try:
        yield factory
    finally:
        engine.dispose()


def _successful_rebuild_result() -> dict[str, object]:
    return {
        "enabled": True,
        "skipped": False,
        "disabled_reason": None,
        "rebuilt": 1,
        "backend": "test",
        "error": None,
    }


def _post_rebuild(factory: sessionmaker[Session], *, rebuild_side_effect=None):  # type: ignore[no-untyped-def]
    app = _make_app(factory)
    with ExitStack() as stack:
        stack.enter_context(patch.object(vector_routes, "build_project_chunks", return_value=[]))
        stack.enter_context(patch.object(vector_routes, "vector_embedding_overrides", return_value={}))
        stack.enter_context(patch.object(vector_routes, "ensure_default_vector_kb"))
        stack.enter_context(patch.object(vector_routes, "get_vector_kb"))
        if rebuild_side_effect is None:
            stack.enter_context(
                patch.object(vector_routes, "rebuild_project", return_value=_successful_rebuild_result())
            )
        else:
            stack.enter_context(patch.object(vector_routes, "rebuild_project", side_effect=rebuild_side_effect))
        return TestClient(app).post(f"/api/projects/{PROJECT_ID}/vector/rebuild", json={})


def test_http_vector_rebuild_clears_dirty_state_when_revision_is_unchanged(session_factory) -> None:  # type: ignore[no-untyped-def]
    response = _post_rebuild(session_factory)

    assert response.status_code == 200
    result = response.json()["data"]["result"]
    assert result["stale"] is False
    assert result["degraded"] is False
    assert "current_dirty_revision" not in result
    with session_factory() as db:
        settings = db.get(ProjectSettings, PROJECT_ID)
        assert settings is not None
        assert settings.vector_index_dirty is False
        assert settings.vector_dirty_revision == 7
        assert settings.last_vector_build_at is not None
        assert settings.last_vector_build_at != datetime(2025, 1, 2, 3, 4, tzinfo=timezone.utc)


def test_http_vector_rebuild_does_not_clear_newer_dirty_revision(session_factory) -> None:  # type: ignore[no-untyped-def]
    old_build_at = datetime(2025, 1, 2, 3, 4, tzinfo=timezone.utc)

    def _concurrent_content_change(**_: object) -> dict[str, object]:
        with session_factory() as concurrent_db:
            assert mark_vector_index_dirty(concurrent_db, project_id=PROJECT_ID) == 8
            concurrent_db.commit()
        return _successful_rebuild_result()

    response = _post_rebuild(session_factory, rebuild_side_effect=_concurrent_content_change)

    assert response.status_code == 200
    result = response.json()["data"]["result"]
    assert result["stale"] is True
    assert result["degraded"] is True
    assert result["current_dirty_revision"] == 8
    with session_factory() as db:
        settings = db.get(ProjectSettings, PROJECT_ID)
        assert settings is not None
        assert settings.vector_index_dirty is True
        assert settings.vector_dirty_revision == 8
        assert settings.last_vector_build_at == old_build_at
