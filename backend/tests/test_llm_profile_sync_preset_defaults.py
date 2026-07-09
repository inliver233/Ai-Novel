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

from app.api.routes import llm_preset as llm_preset_routes
from app.api.routes import llm_profiles as llm_profiles_routes
from app.api.routes import projects as projects_routes
from app.core.errors import AppError
from app.db.base import Base
from app.db.session import get_db
from app.llm.utils import default_max_tokens
from app.main import app_error_handler, validation_error_handler
from app.models.llm_preset import LLMPreset
from app.models.llm_profile import LLMProfile
from app.models.llm_task_preset import LLMTaskPreset
from app.models.project import Project
from app.models.project_membership import ProjectMembership
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
    app.include_router(llm_profiles_routes.router, prefix="/api")
    app.include_router(projects_routes.router, prefix="/api")
    app.include_router(llm_preset_routes.router, prefix="/api")

    def _override_get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    return app


class TestLlmProfileSyncPresetDefaults(unittest.TestCase):
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
                ProjectMembership.__table__,
                LLMProfile.__table__,
                LLMPreset.__table__,
                LLMTaskPreset.__table__,
            ],
        )
        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        self.app = _make_test_app(self.SessionLocal)
        self.client = TestClient(self.app)
        self.headers = {"X-Test-User": "u_owner"}

        with self.SessionLocal() as db:
            db.add(User(id="u_owner", display_name="owner"))
            db.add(Project(id="p1", owner_user_id="u_owner", name="Project 1", genre=None, logline=None))
            db.commit()

    def _create_profile(
        self,
        *,
        name: str,
        provider: str = "openai",
        model: str,
        max_tokens: int | None = None,
        timeout_seconds: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> str:
        res = self.client.post(
            "/api/llm_profiles",
            headers=self.headers,
            json={
                "name": name,
                "provider": provider,
                "base_url": None,
                "model": model,
                "max_tokens": max_tokens,
                "timeout_seconds": timeout_seconds,
                "temperature": temperature,
                "top_p": top_p,
            },
        )
        self.assertEqual(res.status_code, 200)
        return str(res.json()["data"]["profile"]["id"])

    def _bind_project_profile(self, profile_id: str) -> None:
        res = self.client.put(
            "/api/projects/p1",
            headers=self.headers,
            json={"llm_profile_id": profile_id},
        )
        self.assertEqual(res.status_code, 200)

    def _save_custom_preset(self, *, max_tokens: int, timeout_seconds: int) -> None:
        res = self.client.put(
            "/api/projects/p1/llm_preset",
            headers=self.headers,
            json={
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
                "temperature": 0.7,
                "top_p": 1.0,
                "max_tokens": max_tokens,
                "presence_penalty": 0.0,
                "frequency_penalty": 0.0,
                "top_k": None,
                "stop": [],
                "timeout_seconds": timeout_seconds,
                "extra": {},
            },
        )
        self.assertEqual(res.status_code, 200)

    def _get_preset(self) -> dict:
        res = self.client.get("/api/projects/p1/llm_preset", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        return dict(res.json()["data"]["llm_preset"])

    def _create_task_preset(self, *, profile_id: str, provider: str, model: str) -> None:
        with self.SessionLocal() as db:
            db.add(
                LLMTaskPreset(
                    project_id="p1",
                    task_key="chapter_generate",
                    llm_profile_id=profile_id,
                    provider=provider,
                    base_url="https://api.openai.com/v1" if provider == "openai" else "https://api.anthropic.com",
                    model=model,
                    temperature=0.3,
                    top_p=0.9,
                    max_tokens=4096,
                    top_k=None,
                    stop_json="[]",
                    timeout_seconds=180,
                    extra_json="{}",
                )
            )
            db.commit()

    def test_profile_update_syncs_full_template_to_bound_project_preset(self) -> None:
        profile_id = self._create_profile(name="Main", model="gpt-4o-mini", max_tokens=4096, timeout_seconds=240)
        self._bind_project_profile(profile_id)
        self._save_custom_preset(max_tokens=8192, timeout_seconds=321)

        update = self.client.put(
            f"/api/llm_profiles/{profile_id}",
            headers=self.headers,
            json={
                "provider": "openai",
                "base_url": None,
                "model": "gpt-4o",
                "temperature": 0.23,
                "top_p": 0.81,
                "max_tokens": 3072,
                "timeout_seconds": 222,
            },
        )
        self.assertEqual(update.status_code, 200)

        preset = self._get_preset()
        self.assertEqual(preset["model"], "gpt-4o")
        self.assertEqual(preset["temperature"], 0.23)
        self.assertEqual(preset["top_p"], 0.81)
        self.assertEqual(preset["max_tokens"], 3072)
        self.assertEqual(preset["timeout_seconds"], 222)

    def test_project_profile_switch_applies_target_profile_template(self) -> None:
        profile_a = self._create_profile(name="Profile A", model="gpt-4o-mini", max_tokens=8192, timeout_seconds=444)
        profile_b = self._create_profile(name="Profile B", model="gpt-4.1-mini", max_tokens=1536, timeout_seconds=96)

        self._bind_project_profile(profile_a)
        self._save_custom_preset(max_tokens=8192, timeout_seconds=444)

        switch = self.client.put(
            "/api/projects/p1",
            headers=self.headers,
            json={"llm_profile_id": profile_b},
        )
        self.assertEqual(switch.status_code, 200)

        preset = self._get_preset()
        self.assertEqual(preset["model"], "gpt-4.1-mini")
        self.assertEqual(preset["max_tokens"], 1536)
        self.assertEqual(preset["timeout_seconds"], 96)

    @pytest.mark.known_issue  # M22/M23：max_tokens 默认值口径漂移（期望 180，实际 1200）
    def test_binding_profile_creates_preset_with_updated_defaults(self) -> None:
        profile_id = self._create_profile(name="Defaults", model="gpt-4o-mini")
        self._bind_project_profile(profile_id)

        preset = self._get_preset()
        self.assertEqual(preset["timeout_seconds"], 180)
        self.assertEqual(preset["max_tokens"], default_max_tokens("openai", "gpt-4o-mini"))
        self.assertEqual(preset["max_tokens"], 12000)

    def test_profile_update_syncs_bound_task_preset_full_template(self) -> None:
        profile_id = self._create_profile(name="Task Profile", model="gpt-4o-mini")
        self._bind_project_profile(profile_id)
        self._create_task_preset(profile_id=profile_id, provider="openai", model="gpt-4o-mini")

        update = self.client.put(
            f"/api/llm_profiles/{profile_id}",
            headers=self.headers,
            json={
                "provider": "anthropic",
                "base_url": None,
                "model": "claude-3-7-sonnet-20250219",
                "temperature": 0.12,
                "top_p": 0.88,
                "max_tokens": 2048,
                "top_k": 32,
                "stop": ["END"],
                "timeout_seconds": 520,
                "extra": {"foo": "bar"},
            },
        )
        self.assertEqual(update.status_code, 200)

        with self.SessionLocal() as db:
            row = db.get(LLMTaskPreset, ("p1", "chapter_generate"))
            self.assertIsNotNone(row)
            self.assertEqual(row.provider, "anthropic")
            self.assertEqual(row.model, "claude-3-7-sonnet-20250219")
            self.assertEqual(row.base_url, "https://api.anthropic.com")
            self.assertEqual(row.temperature, 0.12)
            self.assertEqual(row.top_p, 0.88)
            self.assertEqual(row.max_tokens, 2048)
            self.assertEqual(row.top_k, 32)
            self.assertEqual(row.stop_json, '["END"]')
            self.assertEqual(row.timeout_seconds, 520)
            self.assertEqual(row.extra_json, '{"foo": "bar"}')


if __name__ == "__main__":
    unittest.main()
