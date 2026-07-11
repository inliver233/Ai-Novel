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


class TestVectorEmbeddingDryRunEndpoint(unittest.TestCase):
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
            ],
        )
        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        self.app = _make_test_app(self.SessionLocal)

        self.secret = "sk-test-SECRET1234"
        with self.SessionLocal() as db:
            db.add(User(id="u_owner", display_name="owner"))
            db.add(Project(id="p1", owner_user_id="u_owner", name="Project 1", genre=None, logline=None))
            db.add(
                ProjectSettings(
                    project_id="p1",
                    vector_embedding_provider="openai_compatible",
                    vector_embedding_base_url="http://127.0.0.1:4011/v1",
                    vector_embedding_model="text-embedding-mock",
                    vector_embedding_api_key_ciphertext=self.secret,
                )
            )
            db.commit()

    def test_dry_run_returns_dims_and_never_returns_plain_api_key(self) -> None:
        captured: dict[str, object] = {}

        def _fake_embed_texts(texts: list[str], *, embedding: dict | None = None) -> dict:  # type: ignore[no-untyped-def]
            captured["texts"] = list(texts)
            captured["embedding"] = dict(embedding or {})
            return {
                "enabled": True,
                "disabled_reason": None,
                "provider": "openai_compatible",
                "vectors": [[0.0, 1.0, 2.0]],
                "error": None,
            }

        with patch.object(vector_routes, "embed_texts", side_effect=_fake_embed_texts):
            client = TestClient(self.app)
            resp = client.post(
                "/api/projects/p1/vector/embeddings/dry-run",
                headers={"X-Test-User": "u_owner"},
                json={"text": "hello"},
            )

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        keys = _collect_keys(payload)
        self.assertNotIn("api_key", keys)
        self.assertNotIn(self.secret, json.dumps(payload, ensure_ascii=False))

        result = (payload.get("data") or {}).get("result") or {}
        self.assertEqual(result.get("enabled"), True)
        self.assertEqual(result.get("dims"), 3)
        self.assertIsInstance((result.get("timings_ms") or {}).get("total"), int)

        embedding = result.get("embedding") or {}
        self.assertEqual(embedding.get("provider"), "openai_compatible")
        self.assertEqual(embedding.get("base_url"), "http://127.0.0.1:4011/v1")
        self.assertEqual(embedding.get("model"), "text-embedding-mock")
        self.assertEqual(embedding.get("has_api_key"), True)
        self.assertEqual(embedding.get("masked_api_key"), "sk-****1234")

        passed = captured.get("embedding") or {}
        self.assertEqual((passed.get("api_key") or ""), self.secret)


if __name__ == "__main__":
    unittest.main()
