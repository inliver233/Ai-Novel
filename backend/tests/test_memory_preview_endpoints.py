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

from app.api.routes import memory as memory_routes
from app.core.errors import AppError
from app.db.base import Base
from app.db.session import get_db
from app.main import app_error_handler, validation_error_handler
from app.models.llm_profile import LLMProfile
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_settings import ProjectSettings
from app.models.user import User
from app.services.memory_retrieval_service import retrieve_memory_context_pack


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
    app.include_router(memory_routes.router, prefix="/api")

    def _override_get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    return app


class TestMemoryPreviewEndpoints(unittest.TestCase):
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
                LLMProfile.__table__,
                Outline.__table__,
                Project.__table__,
                ProjectSettings.__table__,
            ],
        )
        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        self.app = _make_test_app(self.SessionLocal)

        with self.SessionLocal() as db:
            db.add(User(id="u_owner", display_name="owner"))
            db.add(Project(id="p1", owner_user_id="u_owner", name="Project 1", genre=None, logline=None))
            db.commit()

    def test_retrieve_memory_context_pack_never_returns_plain_api_key(self) -> None:
        secret = "sk-test-SECRET1234"
        with self.SessionLocal() as db:
            db.add(ProjectSettings(project_id="p1", vector_embedding_api_key_ciphertext=secret))
            db.commit()

            def _fake_vector_status(*, project_id: str, embedding: dict, rerank: dict) -> dict:
                return {
                    "enabled": True,
                    "disabled_reason": None,
                    "embedding": embedding,
                    "api_key": embedding.get("api_key"),
                }

            with patch("app.services.memory_retrieval_service.vector_rag_status", side_effect=_fake_vector_status):
                pack = retrieve_memory_context_pack(
                    db=db,
                    project_id="p1",
                    query_text="hello",
                    section_enabled={
                        "worldbook": False,
                        "story_memory": False,
                        "structured": False,
                        "vector_rag": False,
                        "graph": False,
                        "fractal": False,
                    },
                )
                data = pack.model_dump()

        keys = _collect_keys(data)
        self.assertNotIn("api_key", keys)
        self.assertNotIn(secret, json.dumps(data, ensure_ascii=False))

    def test_memory_preview_api_never_returns_plain_api_key(self) -> None:
        secret = "sk-test-SECRET1234"
        with self.SessionLocal() as db:
            db.add(ProjectSettings(project_id="p1", vector_embedding_api_key_ciphertext=secret))
            db.commit()

        def _fake_vector_status(*, project_id: str, embedding: dict, rerank: dict) -> dict:
            return {
                "enabled": True,
                "disabled_reason": None,
                "embedding": embedding,
                "api_key": embedding.get("api_key"),
            }

        with patch("app.services.memory_retrieval_service.vector_rag_status", side_effect=_fake_vector_status):
            client = TestClient(self.app)
            resp = client.post(
                "/api/projects/p1/memory/preview",
                headers={"X-Test-User": "u_owner"},
                json={
                    "query_text": "hello",
                    "section_enabled": {
                        "worldbook": False,
                        "story_memory": False,
                        "structured": False,
                        "vector_rag": False,
                        "graph": False,
                        "fractal": False,
                    },
                },
            )

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        keys = _collect_keys(payload)
        self.assertNotIn("api_key", keys)
        self.assertNotIn(secret, json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
