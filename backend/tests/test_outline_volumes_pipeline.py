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
    def __init__(self, outline: Outline, project: Project) -> None:
        self._outline = outline
        self._project = project

    def get(self, model, entity_id: str):
        if model is Outline and entity_id == self._outline.id:
            return self._outline
        if model is Project and entity_id == self._project.id:
            return self._project
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

    @pytest.mark.known_issue  # H19：五条管线 SSE/同步逻辑漂移（细纲 max_tokens 同步流式不一致）
    def test_generate_all_detailed_outlines_uses_llm_for_volume_based_outline(self) -> None:
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

        self.assertEqual(llm_calls, ["卷摘要"])
        self.assertTrue(
            any(
                event.get("type") == "volume_complete" and event.get("chapter_count") == 1
                for event in events
            )
        )


if __name__ == "__main__":
    unittest.main()
