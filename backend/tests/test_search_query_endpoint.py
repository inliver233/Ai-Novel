from __future__ import annotations

import unittest
from typing import Generator

import pytest

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
from app.models.project import Project
from app.models.search_index import SearchDocument
from app.models.user import User
from app.services.search_index_service import upsert_search_document


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

        Base.metadata.create_all(
            engine,
            tables=[
                User.__table__,
                Project.__table__,
                SearchDocument.__table__,
            ],
        )
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE VIRTUAL TABLE search_index USING fts5("
                "title,content,"
                "content='search_documents',content_rowid='id',"
                "tokenize='unicode61'"
                ")"
            )

        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        self.app = _make_test_app(self.SessionLocal)

        with self.SessionLocal() as db:
            db.add(User(id="u_owner", display_name="owner"))
            db.add(Project(id="p1", owner_user_id="u_owner", name="Project 1", genre=None, logline=None))
            db.commit()

            upsert_search_document(
                db=db,
                project_id="p1",
                source_type="chapter",
                source_id="c1",
                title="第 1 章：Start",
                content="Hello world",
                url_path="/p1/chapter/c1",
            )
            upsert_search_document(
                db=db,
                project_id="p1",
                source_type="worldbook_entry",
                source_id="w1",
                title="魔法石",
                content="一种神秘的石头",
                url_path="/p1/worldbook/w1",
            )
            db.commit()

    @pytest.mark.known_issue  # H15：搜索查询绕过 FTS 索引、对 chapters 等做全表 LIKE
    def test_query_returns_items_and_supports_source_filter(self) -> None:
        client = TestClient(self.app)

        resp = client.post(
            "/api/projects/p1/search/query",
            headers={"X-Test-User": "u_owner"},
            json={"q": "Hello", "limit": 20, "offset": 0},
        )
        self.assertEqual(resp.status_code, 200)
        items = (resp.json().get("data") or {}).get("items") or []
        self.assertTrue(items)
        self.assertEqual(items[0].get("source_type"), "chapter")

        resp2 = client.post(
            "/api/projects/p1/search/query",
            headers={"X-Test-User": "u_owner"},
            json={"q": "魔法石", "sources": ["worldbook_entry"], "limit": 20, "offset": 0},
        )
        self.assertEqual(resp2.status_code, 200)
        items2 = (resp2.json().get("data") or {}).get("items") or []
        self.assertTrue(items2)
        self.assertEqual({it.get("source_type") for it in items2}, {"worldbook_entry"})
