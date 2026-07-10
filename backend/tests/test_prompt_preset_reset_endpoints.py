from __future__ import annotations

import unittest
from typing import Generator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.api.routes import prompts as prompts_routes
from app.core.errors import AppError
from app.db.base import Base
from app.db.session import get_db
from app.main import app_error_handler, validation_error_handler
from app.models.project import Project
from app.models.project_membership import ProjectMembership
from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
from app.services.prompt_preset_defaults import prompt_template_hash
from app.models.user import User
from app.services.prompt_preset_resources import load_preset_resource


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
    app.include_router(prompts_routes.router, prefix="/api")

    def _override_get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    return app


class TestPromptPresetResetEndpoints(unittest.TestCase):
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
            ],
        )
        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        self.app = _make_test_app(self.SessionLocal)

        with self.SessionLocal() as db:
            db.add_all(
                [
                    User(id="u_owner", display_name="owner"),
                    User(id="u_editor", display_name="editor"),
                ]
            )
            db.add(Project(id="p1", owner_user_id="u_owner", name="Project 1", genre=None, logline=None))
            db.add(ProjectMembership(project_id="p1", user_id="u_editor", role="editor"))
            db.commit()

    def test_prompt_preset_resources_expose_category(self) -> None:
        client = TestClient(self.app)

        presets_resp = client.get("/api/projects/p1/prompt_presets", headers={"X-Test-User": "u_editor"})
        self.assertEqual(presets_resp.status_code, 200)

        res = client.get("/api/projects/p1/prompt_preset_resources", headers={"X-Test-User": "u_editor"})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["ok"])
        resources = body["data"]["resources"]
        self.assertTrue(isinstance(resources, list))
        self.assertGreater(len(resources), 0)

        plan = next((r for r in resources if r.get("key") == "plan_chapter_v1"), None)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.get("category"), "规划")
        self.assertTrue(isinstance(plan.get("preset_id"), str))

    def test_reset_block_and_preset_to_default_resource(self) -> None:
        client = TestClient(self.app)

        presets_resp = client.get("/api/projects/p1/prompt_presets", headers={"X-Test-User": "u_editor"})
        self.assertEqual(presets_resp.status_code, 200)
        presets = presets_resp.json()["data"]["presets"]
        plan_preset = next((p for p in presets if p.get("resource_key") == "plan_chapter_v1"), None)
        self.assertIsNotNone(plan_preset)
        preset_id = plan_preset["id"]

        preset_detail = client.get(f"/api/prompt_presets/{preset_id}", headers={"X-Test-User": "u_editor"})
        self.assertEqual(preset_detail.status_code, 200)
        blocks = preset_detail.json()["data"]["blocks"]
        self.assertGreater(len(blocks), 0)
        block_id = blocks[0]["id"]
        identifier = blocks[0]["identifier"]

        update_resp = client.put(
            f"/api/prompt_blocks/{block_id}",
            headers={"X-Test-User": "u_editor"},
            json={"template": "changed"},
        )
        self.assertEqual(update_resp.status_code, 200)
        self.assertEqual(update_resp.json()["data"]["block"]["template"], "changed")
        self.assertTrue(update_resp.json()["data"]["block"]["resource_template_outdated"])

        reset_block_resp = client.post(
            f"/api/prompt_blocks/{block_id}/reset_to_default",
            headers={"X-Test-User": "u_editor"},
        )
        self.assertEqual(reset_block_resp.status_code, 200)
        reset_block = reset_block_resp.json()["data"]["block"]
        self.assertEqual(reset_block["id"], block_id)

        resource = load_preset_resource("plan_chapter_v1")
        resource_block = next((b for b in resource.blocks if b.identifier == identifier), None)
        self.assertIsNotNone(resource_block)
        self.assertEqual(reset_block["template"], resource_block.template)
        self.assertFalse(reset_block["resource_template_outdated"])

        with self.SessionLocal() as db:
            stored_block = db.get(PromptBlock, block_id)
            self.assertIsNotNone(stored_block)
            assert stored_block is not None
            self.assertEqual(stored_block.origin_template_hash, prompt_template_hash(resource_block.template))
            self.assertFalse(stored_block.resource_template_outdated)

        metadata_update_resp = client.put(
            f"/api/prompt_blocks/{block_id}",
            headers={"X-Test-User": "u_editor"},
            json={"name": "User customized block name"},
        )
        self.assertEqual(metadata_update_resp.status_code, 200)
        self.assertTrue(metadata_update_resp.json()["data"]["block"]["resource_template_outdated"])

        preset_update = client.put(
            f"/api/prompt_presets/{preset_id}",
            headers={"X-Test-User": "u_editor"},
            json={"version": 999, "category": "temp"},
        )
        self.assertEqual(preset_update.status_code, 200)
        self.assertEqual(preset_update.json()["data"]["preset"]["version"], 999)
        self.assertEqual(preset_update.json()["data"]["preset"]["category"], "temp")

        reset_preset_resp = client.post(
            f"/api/prompt_presets/{preset_id}/reset_to_default",
            headers={"X-Test-User": "u_editor"},
        )
        self.assertEqual(reset_preset_resp.status_code, 200)
        reset_preset = reset_preset_resp.json()["data"]["preset"]
        self.assertEqual(reset_preset["version"], resource.version)
        self.assertEqual(reset_preset["category"], resource.category)

        reset_blocks = reset_preset_resp.json()["data"]["blocks"]
        self.assertEqual(len(reset_blocks), len(resource.blocks))
        self.assertTrue(all(block["resource_template_outdated"] is False for block in reset_blocks))

        with self.SessionLocal() as db:
            stored_blocks = (
                db.query(PromptBlock)
                .filter(PromptBlock.preset_id == preset_id)
                .all()
            )
            self.assertTrue(stored_blocks)
            self.assertTrue(
                all(block.origin_template_hash == prompt_template_hash(block.template) for block in stored_blocks)
            )
            self.assertTrue(all(block.resource_template_outdated is False for block in stored_blocks))


if __name__ == "__main__":
    unittest.main()
