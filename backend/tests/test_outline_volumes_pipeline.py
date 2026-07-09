from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.outline import Outline
from app.models.project import Project
from app.services.detailed_outline_generation import app_service as detailed_outline_app_service
from app.services.detailed_outline_generation.models import DetailedOutlineResult
from app.services.outline_parsing_agent.agents.dynamic_agent import _parse_structure
from app.services.outline_parsing_agent.agents.validation_agent import ValidationAgent
from app.services.outline_parsing_agent.models import AgentStepResult


class _DummyDb:
    """Minimal in-memory Session stub: entity lookup via ``get`` plus the
    ``execute``/``add``/``commit`` surface the detailed-outline fast-path uses
    to persist a row. Stored rows are tracked so tests can assert persistence.
    """

    def __init__(self, outline: Outline, project: Project) -> None:
        self._outline = outline
        self._project = project
        self.added: list = []

    def get(self, model, entity_id: str):
        if model is Outline and entity_id == self._outline.id:
            return self._outline
        if model is Project and entity_id == self._project.id:
            return self._project
        return None

    def execute(self, statement):  # noqa: ARG002 -- matches Session.execute
        # Fast-path always finds no pre-existing DetailedOutline row here.
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    def add(self, row) -> None:
        self.added.append(row)

    def commit(self) -> None:  # no-op, rows already held in self.added
        return None


def _empty_step(agent_name: str, key: str) -> AgentStepResult:
    return AgentStepResult(agent_name=agent_name, status="success", data={key: []})


class TestOutlineVolumesPipeline(unittest.TestCase):
    def test_validation_preserves_volumes_and_synthesizes_compat_chapters(self) -> None:
        structure_data = _parse_structure(
            {
                "outline_md": "## 故事弧线",
                "volumes": [{"number": 1, "title": "第一卷", "summary": "卷摘要"}],
            }
        )

        result = ValidationAgent().validate(
            AgentStepResult(agent_name="structure", status="success", data=structure_data),
            _empty_step("character", "characters"),
            _empty_step("entry", "entries"),
        )

        self.assertEqual(result.outline.volumes, [{"number": 1, "title": "第一卷", "summary": "卷摘要"}])
        self.assertEqual(result.outline.chapters, [{"number": 1, "title": "第一卷", "beats": ["卷摘要"]}])

    def test_extract_volumes_from_outline_reads_summary_from_structure(self) -> None:
        outline = Outline(
            id="outline-1",
            project_id="project-1",
            title="测试大纲",
            content_md="",
            structure_json=json.dumps(
                {"volumes": [{"number": 1, "title": "第一卷", "summary": "卷摘要"}]},
                ensure_ascii=False,
            ),
        )

        volumes = detailed_outline_app_service.extract_volumes_from_outline(outline, db=None)

        self.assertEqual(len(volumes), 1)
        self.assertEqual(volumes[0].title, "第一卷")
        self.assertEqual(volumes[0].beats_text, "卷摘要")

    def test_fast_path_persists_volume_with_summary_without_llm(self) -> None:
        # When an outline structure carries a volume WITH a summary, the
        # pipeline takes the documented fast path: it persists the summary as
        # detailed-outline content directly, WITHOUT invoking the LLM, and
        # emits volume_complete with chapter_count==0. This contract test
        # pins that intentional short-circuit (formerly mis-tagged H19, which
        # actually concerns max_tokens sync/stream drift, not LLM invocation).
        outline = Outline(
            id="outline-1",
            project_id="project-1",
            title="测试大纲",
            content_md="# 大纲",
            structure_json=json.dumps(
                {"volumes": [{"number": 1, "title": "第一卷", "summary": "卷摘要"}]},
                ensure_ascii=False,
            ),
        )
        project = Project(id="project-1", owner_user_id="user-1", name="测试项目")
        db = _DummyDb(outline=outline, project=project)

        llm_calls: list[str] = []

        def _fake_resolve_task_llm_config(*args, **kwargs):
            return SimpleNamespace(
                llm_call=SimpleNamespace(provider="openai_compatible", model="gpt-test", params={}),
                api_key="test-api-key",
            )

        def _fake_generate_detailed_outline_for_volume(
            outline,
            volume_info,
            project,
            llm_config,
            api_key,
            request_id,
            user_id,
            db,
            **kwargs,
        ):
            llm_calls.append(volume_info.beats_text)
            return DetailedOutlineResult(
                detailed_outline_id="detail-1",
                volume_number=volume_info.number,
                volume_title=volume_info.title,
                content_md="细纲内容",
                structure={"chapters": [{"number": 1, "title": "第1章", "summary": "章摘要", "beats": ["推进"]}]},
                chapter_count=1,
                run_id="run-1",
            )

        with patch.object(
            detailed_outline_app_service,
            "resolve_task_llm_config",
            side_effect=_fake_resolve_task_llm_config,
        ), patch.object(
            detailed_outline_app_service,
            "generate_detailed_outline_for_volume",
            side_effect=_fake_generate_detailed_outline_for_volume,
        ):
            events = list(
                detailed_outline_app_service.generate_all_detailed_outlines(
                    outline_id=outline.id,
                    project_id=project.id,
                    user_id="user-1",
                    request_id="req-1",
                    db=db,
                )
            )

        # Fast path: the LLM must NOT be invoked for a volume that already
        # carries a summary.
        self.assertEqual(llm_calls, [])
        # The volume must be persisted (one DetailedOutline row added).
        self.assertEqual(len(db.added), 1)
        # volume_complete is emitted with chapter_count 0 (no chapters parsed
        # in the fast path) and the pipeline finishes without error.
        complete_events = [e for e in events if e.get("type") == "volume_complete"]
        self.assertTrue(len(complete_events) == 1)
        self.assertEqual(complete_events[0].get("chapter_count"), 0)
        self.assertTrue(
            any(event.get("type") == "complete" and event.get("total_chapters") == 0 for event in events)
        )


if __name__ == "__main__":
    unittest.main()
