from __future__ import annotations

import json
import unittest
from typing import Generator
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api.routes import vector as vector_routes
from app.core.errors import AppError
from app.db.base import Base
from app.db.session import get_db
from app.main import app_error_handler, validation_error_handler
from app.models.knowledge_base import KnowledgeBase
from app.models.project import Project
from app.models.project_settings import ProjectSettings
from app.models.user import User


def _collect_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        out = set()
        for k, v in value.items():
            out.add(str(k))
            out |= _collect_keys(v)
        return out
    if isinstance(value, list):
        out = set()
        for item in value:
            out |= _collect_keys(item)
        return out
    return set()


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
    app.include_router(vector_routes.router, prefix="/api")

    def _override_get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    return app


class TestVectorRoutesRerankOverrides(unittest.TestCase):
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
                ProjectSettings.__table__,
                KnowledgeBase.__table__,
            ],
        )
        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        self.app = _make_test_app(self.SessionLocal)

        self.secret = "rk-test-SECRET1234"
        with self.SessionLocal() as db:
            db.add(User(id="u_owner", display_name="owner"))
            db.add(Project(id="p1", owner_user_id="u_owner", name="Project 1", genre=None, logline=None))
            db.add(
                ProjectSettings(
                    project_id="p1",
                    vector_rerank_enabled=True,
                    vector_rerank_method="auto",
                    vector_rerank_provider=None,
                    vector_rerank_base_url="http://127.0.0.1:4011",
                    vector_rerank_model="rerank-mock",
                    vector_rerank_api_key_ciphertext=self.secret,
                    vector_rerank_timeout_seconds=12,
                    vector_rerank_hybrid_alpha=0.33,
                )
            )
            db.commit()

    def test_vector_status_uses_full_rerank_overrides_and_never_returns_plain_api_key(self) -> None:
        def _fake_vector_status(*, project_id: str, sources: list[str], embedding: dict, rerank: dict) -> dict:
            return {
                "enabled": True,
                "disabled_reason": None,
                "rerank": rerank,
                "api_key": rerank.get("api_key"),
            }

        with patch.object(vector_routes, "vector_rag_status", side_effect=_fake_vector_status):
            client = TestClient(self.app)
            resp = client.post(
                "/api/projects/p1/vector/status",
                headers={"X-Test-User": "u_owner"},
                json={},
            )

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        keys = _collect_keys(payload)
        self.assertNotIn("api_key", keys)
        self.assertNotIn(self.secret, json.dumps(payload, ensure_ascii=False))

        rerank = ((payload.get("data") or {}).get("result") or {}).get("rerank") or {}
        self.assertEqual(rerank.get("provider"), "external_rerank_api")
        self.assertEqual(rerank.get("base_url"), "http://127.0.0.1:4011")
        self.assertEqual(rerank.get("model"), "rerank-mock")
        self.assertEqual(rerank.get("timeout_seconds"), 12.0)
        self.assertEqual(rerank.get("hybrid_alpha"), 0.33)
        self.assertEqual(rerank.get("has_api_key"), True)
        self.assertEqual(rerank.get("masked_api_key"), "rk-****1234")

    def test_vector_query_uses_full_rerank_overrides_and_never_returns_plain_api_key(self) -> None:
        def _fake_query_project(*, query_text: str, rerank: dict | None = None, **_: object) -> dict:
            return {
                "enabled": True,
                "query_text": query_text,
                "rerank": rerank or {},
                "api_key": (rerank or {}).get("api_key"),
            }

        with patch.object(vector_routes, "query_project", side_effect=_fake_query_project):
            client = TestClient(self.app)
            resp = client.post(
                "/api/projects/p1/vector/query",
                headers={"X-Test-User": "u_owner"},
                json={"query_text": "hello"},
            )

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        keys = _collect_keys(payload)
        self.assertNotIn("api_key", keys)
        self.assertNotIn(self.secret, json.dumps(payload, ensure_ascii=False))

        rerank = ((payload.get("data") or {}).get("result") or {}).get("rerank") or {}
        self.assertEqual(rerank.get("provider"), "external_rerank_api")
        self.assertEqual(rerank.get("base_url"), "http://127.0.0.1:4011")
        self.assertEqual(rerank.get("model"), "rerank-mock")
        self.assertEqual(rerank.get("timeout_seconds"), 12.0)
        self.assertEqual(rerank.get("hybrid_alpha"), 0.33)
        self.assertEqual(rerank.get("has_api_key"), True)
        self.assertEqual(rerank.get("masked_api_key"), "rk-****1234")


if __name__ == "__main__":
    unittest.main()
