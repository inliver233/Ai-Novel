from __future__ import annotations

import unittest
from typing import Generator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api.routes import search as search_routes
from app.core.errors import AppError
from app.db.base import Base
from app.db.session import get_db
from app.main import app_error_handler, validation_error_handler
from app.models.chapter import Chapter
from app.models.project import Project
from app.models.user import User


def _make_test_app(SessionLocal: sessionmaker) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _test_user_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = "rid-test"
        user_id = request.headers.get("X-Test-User")
        request.state.user_id = user_id
        request.state.authenticated_user_id = user_id
        request.state.session_expire_at = None
        request.state.auth_source = "test"
        return await call_next(request)

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.include_router(search_routes.router, prefix="/api")

    def _override_get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    return app


class TestSearchQueryEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.addCleanup(engine.dispose)

        # 当前实现读取 Chapter；未来切换 search_documents/FTS 后可调整 fixture，但本测试
        # 只断言 HTTP 层的搜索结果与 source filter 语义，不锁内部 mode/SQL 路径。
        Base.metadata.create_all(
            engine,
            tables=[
                User.__table__,
                Project.__table__,
                Chapter.__table__,
            ],
        )

        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        self.app = _make_test_app(self.SessionLocal)

        with self.SessionLocal() as db:
            db.add(User(id="u_owner", display_name="owner"))
            db.add(Project(id="p1", owner_user_id="u_owner", name="Project 1", genre=None, logline=None))
            db.add(
                Chapter(
                    id="c1",
                    project_id="p1",
                    outline_id="o1",
                    number=1,
                    title="Start",
                    content_md="Hello world",
                )
            )
            db.commit()

    def test_query_returns_items_and_supports_source_filter(self) -> None:
        client = TestClient(self.app)

        # A query scoped to a valid source type returns matching items from the
        # underlying business table.
        resp = client.post(
            "/api/projects/p1/search/query",
            headers={"X-Test-User": "u_owner"},
            json={"q": "Hello", "sources": ["chapter"], "limit": 20, "offset": 0},
        )
        self.assertEqual(resp.status_code, 200)
        items = (resp.json().get("data") or {}).get("items") or []
        self.assertTrue(items)
        self.assertEqual(items[0].get("source_type"), "chapter")

        # A source filter naming no known source type is dropped to an empty search
        # set and returns an empty result gracefully (no error).
        resp2 = client.post(
            "/api/projects/p1/search/query",
            headers={"X-Test-User": "u_owner"},
            json={"q": "Hello", "sources": ["worldbook_entry"], "limit": 20, "offset": 0},
        )
        self.assertEqual(resp2.status_code, 200)
        items2 = (resp2.json().get("data") or {}).get("items") or []
        self.assertEqual(items2, [])
