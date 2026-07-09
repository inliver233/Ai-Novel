from __future__ import annotations

import json
import os
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.core.errors import AppError
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.llm_preset import LLMPreset
from app.models.outline import Outline
from app.models.project import Project
from app.models.user import User
from app.services.characters_auto_update_service import characters_auto_update_v1
from app.services.generation_service import RecordedLlmResult
from app.services.project_task_service import schedule_chapter_done_tasks


def _compact_json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class TestCharactersAutoUpdateService(unittest.TestCase):
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
                Outline.__table__,
                Chapter.__table__,
                Character.__table__,
                LLMPreset.__table__,
            ],
        )
        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        with self.SessionLocal() as db:
            db.add(User(id="u1", display_name="u1"))
            db.add(Project(id="p1", owner_user_id="u1", name="P1", genre=None, logline=None))
            db.add(Outline(id="o1", project_id="p1", title="Outline", content_md="outline", structure_json=None))
            db.add(
                Chapter(
                    id="c1",
                    project_id="p1",
                    outline_id="o1",
                    number=1,
                    title="Ch1",
                    plan=None,
                    content_md="Alice meets Bob.",
                    summary=None,
                    status="done",
                )
            )
            db.add(LLMPreset(project_id="p1", provider="openai", base_url=None, model="gpt-test"))
            db.add(Character(id="ch-alice", project_id="p1", name="Alice", role=None, profile="existing", notes=None))
            db.commit()

    def test_characters_auto_update_v1_upserts_and_merges(self) -> None:
        model_out = _compact_json_dumps(
            {
                "schema_version": "characters_auto_update_v1",
                "title": "Characters Auto Update",
                "summary_md": "auto",
                "ops": [
                    {
                        "op": "upsert",
                        "name": "Alice",
                        "patch": {"role": "hero", "profile": "NEW PROFILE", "notes": "n1"},
                        "merge_mode_profile": "append_missing",
                        "merge_mode_notes": "append_missing",
                        "reason": "Alice appears in chapter",
                    },
                    {
                        "op": "upsert",
                        "name": "Bob",
                        "patch": {"role": "sidekick", "profile": "Bob profile", "notes": ""},
                        "reason": "Bob appears in chapter",
                    },
                ],
            }
        )

        with patch("app.services.characters_auto_update_service.SessionLocal", self.SessionLocal), patch(
            "app.services.characters_auto_update_service.resolve_api_key_for_project", return_value="masked_api_key"
        ), patch(
            "app.services.llm_retry.call_llm_and_record",
            return_value=RecordedLlmResult(
                text=model_out,
                finish_reason=None,
                latency_ms=1,
                dropped_params=[],
                run_id="run-test",
            ),
        ), patch("app.services.characters_auto_update_service.schedule_search_rebuild_task", return_value=None):
            res = characters_auto_update_v1(project_id="p1", actor_user_id="u1", request_id="rid-test", chapter_id="c1")

        self.assertTrue(bool(res.get("ok")))

        with self.SessionLocal() as db:
            rows = (
                db.execute(select(Character).where(Character.project_id == "p1").order_by(Character.name.asc())).scalars().all()
            )
            self.assertEqual([r.name for r in rows], ["Alice", "Bob"])

            alice = next(r for r in rows if r.name == "Alice")
            self.assertEqual(alice.role, "hero")
            # append_missing: should NOT overwrite existing
            self.assertEqual(alice.profile, "existing")
            self.assertEqual(alice.notes, "n1")

            bob = next(r for r in rows if r.name == "Bob")
            self.assertEqual(bob.role, "sidekick")
            self.assertEqual(bob.profile, "Bob profile")

    def test_characters_auto_update_v1_repairs_invalid_json_once(self) -> None:
        repaired = {
            "schema_version": "characters_auto_update_v1",
            "title": "Characters Auto Update",
            "summary_md": "auto",
            "ops": [
                {
                    "op": "upsert",
                    "name": "Bob",
                    "patch": {"role": "sidekick", "profile": "Bob profile", "notes": ""},
                    "reason": "Bob appears in chapter",
                }
            ],
        }

        with patch("app.services.characters_auto_update_service.SessionLocal", self.SessionLocal), patch(
            "app.services.characters_auto_update_service.resolve_api_key_for_project", return_value="masked_api_key"
        ), patch(
            "app.services.llm_retry.call_llm_and_record",
            return_value=RecordedLlmResult(
                text="not json",
                finish_reason=None,
                latency_ms=1,
                dropped_params=[],
                run_id="run-orig",
            ),
        ), patch(
            "app.services.characters_auto_update_service.repair_json_once",
            return_value={"ok": True, "repair_run_id": "run-repair", "value": repaired, "raw_json": _compact_json_dumps(repaired)},
        ), patch("app.services.characters_auto_update_service.schedule_search_rebuild_task", return_value=None):
            res = characters_auto_update_v1(project_id="p1", actor_user_id="u1", request_id="rid-test", chapter_id="c1")

        self.assertTrue(bool(res.get("ok")))
        self.assertEqual(res.get("run_id"), "run-orig")
        self.assertEqual(res.get("repair_run_id"), "run-repair")

        with self.SessionLocal() as db:
            rows = (
                db.execute(select(Character).where(Character.project_id == "p1").order_by(Character.name.asc())).scalars().all()
            )
            self.assertEqual([r.name for r in rows], ["Alice", "Bob"])

    def test_characters_auto_update_v1_retries_once_on_timeout_and_succeeds(self) -> None:
        model_out = _compact_json_dumps(
            {
                "schema_version": "characters_auto_update_v1",
                "title": "Characters Auto Update",
                "summary_md": "auto",
                "ops": [
                    {
                        "op": "upsert",
                        "name": "Bob",
                        "patch": {"role": "sidekick", "profile": "Bob profile", "notes": ""},
                        "reason": "Bob appears in chapter",
                    }
                ],
            }
        )

        timeout_exc = AppError(code="LLM_TIMEOUT", message="timeout", status_code=504, details={"run_id": "run-orig"})
        ok = RecordedLlmResult(text=model_out, finish_reason=None, latency_ms=1, dropped_params=[], run_id="run-retry")

        with patch.dict(
            os.environ,
            {"TASK_LLM_MAX_ATTEMPTS": "2", "TASK_LLM_RETRY_BASE_SECONDS": "0", "TASK_LLM_RETRY_JITTER": "0"},
            clear=False,
        ), patch("app.services.characters_auto_update_service.SessionLocal", self.SessionLocal), patch(
            "app.services.characters_auto_update_service.resolve_api_key_for_project", return_value="masked_api_key"
        ), patch(
            "app.services.llm_retry.call_llm_and_record",
            side_effect=[timeout_exc, ok],
        ) as mock_call, patch("app.services.characters_auto_update_service.schedule_search_rebuild_task", return_value=None):
            res = characters_auto_update_v1(project_id="p1", actor_user_id="u1", request_id="rid-test", chapter_id="c1")

        self.assertTrue(bool(res.get("ok")))
        self.assertEqual(res.get("run_id"), "run-retry")
        self.assertEqual(mock_call.call_count, 2)
        self.assertTrue(str(mock_call.call_args_list[1].kwargs.get("request_id") or "").endswith(":retry1"))

    def test_schedule_chapter_done_tasks_includes_characters_auto_update(self) -> None:
        # lite 分支删除了 worldbook / fractal auto-update；当前活调度函数为
        # vector_rebuild / search_rebuild / characters_auto_update。
        with patch("app.services.vector_rag_service.schedule_vector_rebuild_task", return_value="t-vector"), patch(
            "app.services.search_index_service.schedule_search_rebuild_task", return_value="t-search"
        ), patch(
            "app.services.characters_auto_update_service.schedule_characters_auto_update_task", return_value="t-characters"
        ):
            out = schedule_chapter_done_tasks(
                db=Mock(),
                project_id="p1",
                actor_user_id="u1",
                request_id="rid-test",
                chapter_id="c1",
                chapter_token="2026-02-01T00:00:00Z",
                reason="chapter_done",
            )

        self.assertEqual(out.get("characters_auto_update"), "t-characters")
