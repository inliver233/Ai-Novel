from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.errors import AppError
from app.db.base import Base
from app.models.batch_generation_task import BatchGenerationTask, BatchGenerationTaskItem
from app.models.chapter import Chapter
from app.models.generation_run import GenerationRun
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_task import ProjectTask
from app.models.project_task_event import ProjectTaskEvent
from app.models.user import User
from app.services import batch_generation_service


class TestBatchGenerationWorkerPauseRecovery(unittest.TestCase):
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
                GenerationRun.__table__,
                Project.__table__,
                Outline.__table__,
                Chapter.__table__,
                ProjectTask.__table__,
                ProjectTaskEvent.__table__,
                BatchGenerationTask.__table__,
            ],
        )
        BatchGenerationTaskItem.__table__.create(bind=engine)
        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        with self.SessionLocal() as db:
            db.add(User(id="u_owner", display_name="owner"))
            db.add(Project(id="p1", owner_user_id="u_owner", active_outline_id="o1", name="Project 1", genre=None, logline=None))
            db.add(Outline(id="o1", project_id="p1", title="Outline", content_md="", structure_json=None))
            db.add(Chapter(id="c1", project_id="p1", outline_id="o1", number=1, title="第一章", plan="计划", content_md="seed", summary="seed"))
            db.add(
                ProjectTask(
                    id="pt-batch",
                    project_id="p1",
                    actor_user_id="u_owner",
                    kind="batch_generation_orchestrator",
                    status="queued",
                    idempotency_key="batch_generation:task-1",
                    params_json=json.dumps({"batch_task_id": "task-1"}, ensure_ascii=False),
                    result_json=None,
                    error_json=None,
                )
            )
            db.add(
                BatchGenerationTask(
                    id="task-1",
                    project_id="p1",
                    outline_id="o1",
                    actor_user_id="u_owner",
                    project_task_id="pt-batch",
                    status="queued",
                    total_count=1,
                    completed_count=0,
                    failed_count=0,
                    skipped_count=0,
                    cancel_requested=False,
                    pause_requested=False,
                    params_json=json.dumps(
                        {
                            "instruction": "",
                            "target_word_count": None,
                            "plan_first": False,
                            "post_edit": False,
                            "post_edit_sanitize": False,
                            "content_optimize": False,
                            "style_id": None,
                            "context": {
                                "include_world_setting": False,
                                "include_style_guide": False,
                                "include_constraints": False,
                                "include_outline": False,
                                "include_smart_context": False,
                                "character_ids": [],
                                "previous_chapter": "none",
                            },
                        },
                        ensure_ascii=False,
                    ),
                    checkpoint_json=None,
                    error_json=None,
                )
            )
            db.add(
                BatchGenerationTaskItem(
                    id="item-1",
                    task_id="task-1",
                    chapter_id="c1",
                    chapter_number=1,
                    status="queued",
                    attempt_count=0,
                    generation_run_id=None,
                    last_request_id=None,
                    error_message=None,
                    last_error_json=None,
                    started_at=None,
                    finished_at=None,
                )
            )
            db.commit()

    def test_worker_failure_pauses_batch_and_runtime(self) -> None:
        fake_project = SimpleNamespace(id="p1")
        fake_call = SimpleNamespace(provider="mock", model="mock")

        with patch.object(batch_generation_service, "SessionLocal", self.SessionLocal), patch.object(
            batch_generation_service,
            "_prepare_project_context",
            return_value=(fake_project, fake_call, "sk-test", "", "", "", "", "", {}),
        ), patch.object(
            batch_generation_service,
            "assemble_chapter_generate_render_values",
            return_value=({}, {}),
        ), patch.object(
            batch_generation_service,
            "render_preset_for_task",
            return_value=("sys", "user", None, None, None, None, {}),
        ), patch.object(
            batch_generation_service,
            # 抑制心跳：touch_batch_project_task 内部会调 touch_project_task_heartbeat，
            # 后者经未打补丁的 SessionLocal 写真实库（测试内存库无对应线程上下文）。
            "touch_batch_project_task",
            return_value=None,
        ), patch.object(
            batch_generation_service,
            "run_chapter_generate_llm_step",
            side_effect=AppError(code="MOCK_FAIL", message="mock fail", status_code=503),
        ):
            batch_generation_service.run_batch_generation_task(task_id="task-1")

        with self.SessionLocal() as db:
            task = db.get(BatchGenerationTask, "task-1")
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.status, "paused")
            self.assertTrue(bool(task.pause_requested))
            self.assertEqual(task.failed_count, 1)

            item = db.get(BatchGenerationTaskItem, "item-1")
            self.assertIsNotNone(item)
            assert item is not None
            self.assertEqual(item.status, "failed")
            self.assertTrue(str(item.error_message or "").strip())
            self.assertTrue(str(item.last_error_json or "").strip())

            runtime_task = db.get(ProjectTask, "pt-batch")
            self.assertIsNotNone(runtime_task)
            assert runtime_task is not None
            self.assertEqual(runtime_task.status, "paused")
            self.assertTrue(str(runtime_task.error_json or "").strip())

            event_types = db.execute(
                select(ProjectTaskEvent.event_type)
                .where(ProjectTaskEvent.task_id == "pt-batch")
                .order_by(ProjectTaskEvent.seq.asc())
            ).scalars().all()
            self.assertEqual(event_types, ["running", "step_started", "step_failed", "paused"])
