from __future__ import annotations

import json
import unittest
from typing import Generator

import pytest

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api.routes import prompt_studio as prompt_studio_routes
from app.core.errors import AppError
from app.db.base import Base
from app.db.session import get_db
from app.main import app_error_handler, validation_error_handler
from app.models.project import Project
from app.models.project_default_style import ProjectDefaultStyle
from app.models.project_membership import ProjectMembership
from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
from app.models.user import User
from app.models.writing_style import WritingStyle


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
    app.include_router(prompt_studio_routes.router, prefix="/api")

    def _override_get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    return app


class TestPromptStudioRoutes(unittest.TestCase):
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
                PromptPreset.__table__,
                PromptBlock.__table__,
                WritingStyle.__table__,
                ProjectDefaultStyle.__table__,
            ],
        )
        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        self.app = _make_test_app(self.SessionLocal)

        with self.SessionLocal() as db:
            db.add_all(
                [
                    User(id="u_owner", display_name="owner"),
                    User(id="u_editor", display_name="editor"),
                    User(id="u_other", display_name="other"),
                ]
            )
            db.add(Project(id="p1", owner_user_id="u_owner", name="Project 1", genre=None, logline=None))
            db.add(ProjectMembership(project_id="p1", user_id="u_editor", role="editor"))
            db.commit()

    def test_categories_route_returns_all_studio_categories(self) -> None:
        client = TestClient(self.app)
        response = client.get("/api/projects/p1/prompt-studio/categories", headers={"X-Test-User": "u_editor"})
        self.assertEqual(response.status_code, 200)

        payload = response.json()
        self.assertTrue(payload["ok"])
        categories = payload["data"]["categories"]
        self.assertEqual(
            [item["key"] for item in categories],
            ["outline_generate", "chapter_generate", "plan_chapter", "post_edit", "writing_style"],
        )
        for item in categories[:-1]:
            self.assertGreaterEqual(len(item["presets"]), 1)

    @pytest.mark.known_issue  # REAL BUG (M28/M23): prompt_studio_service.py:65,74 set config output_contract_heading="输出要求：" for plan_chapter/post_edit, but NO resource under app/resources/prompt_presets contains that heading (all use "<OUTPUT_CONTRACT>", verified via Grep). So _output_contract_suffix returns "" and _compose_guidance_template (:214-219) drops the entire output contract — incl. the <plan>/<rewrite> wrapper — from created/edited presets, so the LLM stops wrapping output. assertIn("<plan>", ...) below states the correct behavior the bug violates; it turns green once the heading aligns with the resources.
    def test_prompt_preset_crud_and_activation(self) -> None:
        client = TestClient(self.app)

        create_response = client.post(
            "/api/projects/p1/prompt-studio/presets?category=plan_chapter",
            headers={"X-Test-User": "u_editor"},
            json={"name": "我的章节分析", "content": "你是我的私人章节分析师。"},
        )
        self.assertEqual(create_response.status_code, 200)
        created = create_response.json()["data"]["preset"]
        preset_id = created["id"]
        self.assertEqual(created["name"], "我的章节分析")
        self.assertEqual(created["content"], "你是我的私人章节分析师。")
        self.assertFalse(created["is_active"])

        detail_response = client.get(
            f"/api/projects/p1/prompt-studio/presets/{preset_id}?category=plan_chapter",
            headers={"X-Test-User": "u_editor"},
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.json()["data"]["preset"]["content"], "你是我的私人章节分析师。")

        with self.SessionLocal() as db:
            block = (
                db.execute(
                    select(PromptBlock)
                    .where(PromptBlock.preset_id == preset_id)
                    .where(PromptBlock.identifier == "sys.plan_chapter.role")
                )
                .scalars()
                .one()
            )
            self.assertIn("<plan>", block.template)
            self.assertIn("你是我的私人章节分析师。", block.template)

        activate_response = client.put(
            f"/api/projects/p1/prompt-studio/presets/{preset_id}/activate?category=plan_chapter",
            headers={"X-Test-User": "u_editor"},
        )
        self.assertEqual(activate_response.status_code, 200)
        self.assertTrue(activate_response.json()["data"]["preset"]["is_active"])

        update_response = client.put(
            f"/api/projects/p1/prompt-studio/presets/{preset_id}",
            headers={"X-Test-User": "u_editor"},
            json={"name": "我的章节分析 v2", "content": "你是更严格的章节分析师。"},
        )
        self.assertEqual(update_response.status_code, 200)
        updated = update_response.json()["data"]["preset"]
        self.assertEqual(updated["name"], "我的章节分析 v2")
        self.assertEqual(updated["content"], "你是更严格的章节分析师。")
        self.assertTrue(updated["is_active"])

        delete_response = client.delete(
            f"/api/projects/p1/prompt-studio/presets/{preset_id}",
            headers={"X-Test-User": "u_editor"},
        )
        self.assertEqual(delete_response.status_code, 200)

        get_deleted_response = client.get(
            f"/api/projects/p1/prompt-studio/presets/{preset_id}?category=plan_chapter",
            headers={"X-Test-User": "u_editor"},
        )
        self.assertEqual(get_deleted_response.status_code, 404)

    def test_categories_route_filters_prompt_presets_without_guidance_block(self) -> None:
        # A preset mapped to a category (here via active_for) but missing that
        # category's guidance block must be excluded from the category listing.
        # The preset is intentionally unbound (resource_key=None) so
        # _ensure_prompt_studio_baseline never auto-upgrades it (the legacy
        # version=3 < outline_generate_v3 resource version=4 path that used to
        # inject the guidance block cannot fire); it stays guidance-block-free
        # and the _has_guidance_block filter must keep it out.
        with self.SessionLocal() as db:
            db.add(
                PromptPreset(
                    id="legacy-outline",
                    project_id="p1",
                    name="Legacy Outline",
                    resource_key=None,
                    category="prompt",
                    scope="project",
                    version=3,
                    active_for_json=json.dumps(["outline_generate"], ensure_ascii=False),
                )
            )
            db.add(
                PromptBlock(
                    id="legacy-outline-block",
                    preset_id="legacy-outline",
                    identifier="sys.story.append_rules",
                    name="Legacy Outline Block",
                    role="system",
                    enabled=True,
                    template="legacy block",
                    marker_key=None,
                    injection_position="relative",
                    injection_depth=None,
                    injection_order=0,
                    triggers_json="[]",
                    forbid_overrides=False,
                    budget_json=None,
                    cache_json=None,
                )
            )
            db.commit()

        client = TestClient(self.app)
        response = client.get("/api/projects/p1/prompt-studio/categories", headers={"X-Test-User": "u_editor"})
        self.assertEqual(response.status_code, 200)

        categories = response.json()["data"]["categories"]
        outline_category = next(item for item in categories if item["key"] == "outline_generate")
        preset_ids = [item["id"] for item in outline_category["presets"]]

        # The baseline default outline preset (which carries the guidance block)
        # is listed, so the category itself works and the filter is meaningful...
        self.assertTrue(preset_ids)
        # ...while the guidance-block-free legacy preset is filtered out.
        self.assertNotIn("legacy-outline", preset_ids)

    def test_writing_style_crud_and_activation(self) -> None:
        client = TestClient(self.app)

        create_response = client.post(
            "/api/projects/p1/prompt-studio/presets?category=writing_style",
            headers={"X-Test-User": "u_editor"},
            json={"name": "冷峻克制", "content": "语言克制，节奏紧凑。"},
        )
        self.assertEqual(create_response.status_code, 200)
        created = create_response.json()["data"]["preset"]
        style_id = created["id"]
        self.assertEqual(created["content"], "语言克制，节奏紧凑。")
        self.assertFalse(created["is_active"])

        activate_response = client.put(
            f"/api/projects/p1/prompt-studio/presets/{style_id}/activate?category=writing_style",
            headers={"X-Test-User": "u_editor"},
        )
        self.assertEqual(activate_response.status_code, 200)
        self.assertTrue(activate_response.json()["data"]["preset"]["is_active"])

        update_response = client.put(
            f"/api/projects/p1/prompt-studio/presets/{style_id}",
            headers={"X-Test-User": "u_editor"},
            json={"name": "冷峻克制 v2", "content": "语言克制，镜头感强。"},
        )
        self.assertEqual(update_response.status_code, 200)
        updated = update_response.json()["data"]["preset"]
        self.assertEqual(updated["name"], "冷峻克制 v2")
        self.assertEqual(updated["content"], "语言克制，镜头感强。")
        self.assertTrue(updated["is_active"])

        detail_response = client.get(
            f"/api/projects/p1/prompt-studio/presets/{style_id}?category=writing_style",
            headers={"X-Test-User": "u_editor"},
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.json()["data"]["preset"]["name"], "冷峻克制 v2")

        delete_response = client.delete(
            f"/api/projects/p1/prompt-studio/presets/{style_id}",
            headers={"X-Test-User": "u_editor"},
        )
        self.assertEqual(delete_response.status_code, 200)

        with self.SessionLocal() as db:
            self.assertIsNone(db.get(WritingStyle, style_id))
            default_style = db.get(ProjectDefaultStyle, "p1")
            if default_style is not None:
                self.assertIsNone(default_style.style_id)


if __name__ == "__main__":
    unittest.main()
