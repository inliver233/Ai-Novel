from __future__ import annotations

import json
import re
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.api.routes.outline import (
    OUTLINE_SEGMENT_INDEX_MAX_CHARS,
    _build_outline_missing_chapters_prompts,
    _build_outline_segment_chapter_index,
    _build_outline_segment_prompts,
    _build_outline_generation_guidance,
    _enforce_outline_chapter_coverage,
    _extract_target_chapter_count,
    _format_chapter_number_ranges,
    _outline_fill_batch_size_for_missing,
    _outline_fill_max_attempts_for_missing,
    _outline_fill_progress_message,
    _outline_segment_batch_size_for_target,
    _parse_outline_batch_output,
    _recommend_outline_max_tokens,
    _strip_segment_conflicting_prompt_sections,
    _should_use_outline_segmented_mode,
)
from app.core.errors import AppError
from app.llm.capabilities import max_output_tokens_limit
from app.services.generation_service import PreparedLlmCall
from app.services.outline_generation.app_service import (
    _fill_outline_missing_chapters_with_llm,
    _generate_outline_segmented_with_llm,
)
from app.services.prompting import render_template


class TestOutlineGenerationGuidance(unittest.TestCase):
    def test_extract_target_chapter_count(self) -> None:
        self.assertEqual(_extract_target_chapter_count({"chapter_count": 200}), 200)
        self.assertEqual(_extract_target_chapter_count({"chapter_count": "120"}), 120)
        self.assertIsNone(_extract_target_chapter_count({"chapter_count": "abc"}))
        self.assertIsNone(_extract_target_chapter_count({"chapter_count": 0}))
        self.assertIsNone(_extract_target_chapter_count({}))
        self.assertIsNone(_extract_target_chapter_count(None))

    def test_build_outline_generation_guidance_for_long_form(self) -> None:
        guidance = _build_outline_generation_guidance(200)
        self.assertIn("200", guidance["chapter_count_rule"])
        self.assertIn("每章 1 条", guidance["chapter_detail_rule"])

    def test_build_outline_generation_guidance_for_50_chapters(self) -> None:
        guidance = _build_outline_generation_guidance(50)
        self.assertIn("50", guidance["chapter_count_rule"])
        self.assertIn("1~2", guidance["chapter_detail_rule"])

    def test_build_outline_generation_guidance_default(self) -> None:
        guidance = _build_outline_generation_guidance(None)
        self.assertEqual(guidance["chapter_count_rule"], "")
        self.assertIn("5~9", guidance["chapter_detail_rule"])

    def test_should_use_outline_segmented_mode(self) -> None:
        self.assertFalse(_should_use_outline_segmented_mode(None))
        self.assertFalse(_should_use_outline_segmented_mode(79))
        self.assertTrue(_should_use_outline_segmented_mode(80))
        self.assertTrue(_should_use_outline_segmented_mode(500))

    def test_outline_segment_batch_size_for_target(self) -> None:
        self.assertEqual(_outline_segment_batch_size_for_target(60), 12)
        self.assertEqual(_outline_segment_batch_size_for_target(200), 10)
        self.assertEqual(_outline_segment_batch_size_for_target(900), 8)

    def test_recommend_outline_max_tokens(self) -> None:
        # Properties of the max_tokens recommender (floor = chapters*200 clamped to
        # the model output-token limit, only raises, never lowers). No spec mandates
        # specific magic numbers, so we assert structural invariants instead.
        provider = "openai"
        model = "gpt-4o-mini"
        limit = max_output_tokens_limit(provider, model)
        self.assertIsInstance(limit, int)
        self.assertGreater(limit, 0)

        # Small chapter counts (<=20) never override max_tokens.
        self.assertIsNone(
            _recommend_outline_max_tokens(
                target_chapter_count=20,
                provider=provider,
                model=model,
                current_max_tokens=4096,
            )
        )

        # Across a growing chapter count the recommendation is monotonically
        # non-decreasing and never exceeds the model output-token limit.
        previous: int | None = None
        for count in (21, 40, 50, 80, 120, 200, 320, 500):
            recommended = _recommend_outline_max_tokens(
                target_chapter_count=count,
                provider=provider,
                model=model,
                current_max_tokens=4096,
            )
            if recommended is not None:
                self.assertLessEqual(recommended, limit)
                if previous is not None:
                    self.assertGreaterEqual(recommended, previous)
                previous = recommended

        # Only raises, never lowers: when current_max_tokens already meets the
        # recommendation, no override is returned.
        self.assertIsNone(
            _recommend_outline_max_tokens(
                target_chapter_count=200,
                provider=provider,
                model=model,
                current_max_tokens=limit,
            )
        )

        # A model with a smaller output-token cap (gpt-4=8192) clamps the
        # recommendation to its own limit rather than exceeding it.
        gpt4_limit = max_output_tokens_limit("openai", "gpt-4")
        self.assertEqual(gpt4_limit, 8192)
        clamped = _recommend_outline_max_tokens(
            target_chapter_count=200,
            provider="openai",
            model="gpt-4",
            current_max_tokens=4096,
        )
        self.assertIsNotNone(clamped)
        self.assertLessEqual(clamped, gpt4_limit)

    def test_outline_contract_template_uses_dynamic_rules(self) -> None:
        template_path = Path("app/resources/prompt_presets/outline_generate_v3/templates/sys.outline.contract.json.md")
        template = template_path.read_text(encoding="utf-8")

        # Structural: this VOLUME contract carries the chapter_count_rule dynamic
        # placeholder (it is a per-volume contract, not a per-chapter beats template,
        # so chapter_detail_rule is intentionally absent).
        self.assertIn("{{chapter_count_rule}}", template)

        # When chapter_count_rule is supplied it renders into the contract output.
        rendered, _missing, error = render_template(
            template,
            values={
                "chapter_count_rule": "chapters 必须输出 200 章，number 需完整覆盖 1..200 且不缺号。",
            },
            macro_seed="test-seed",
        )
        self.assertIsNone(error)
        self.assertIn("200 章", rendered)

    def test_enforce_outline_chapter_coverage_marks_missing_numbers_without_padding(self) -> None:
        data = {
            "outline_md": "x",
            "chapters": [
                {"number": 1, "title": "第一章", "beats": ["a"]},
                {"number": 3, "title": "第三章", "beats": ["c"]},
            ],
        }
        out, warnings = _enforce_outline_chapter_coverage(data=data, target_chapter_count=4)
        chapters = out["chapters"]
        self.assertEqual([c["number"] for c in chapters], [1, 3])
        self.assertIn("outline_chapter_coverage_incomplete", warnings)
        coverage = out.get("chapter_coverage") or {}
        self.assertEqual(coverage.get("missing_numbers"), [2, 4])
        self.assertEqual(coverage.get("missing_count"), 2)

    def test_enforce_outline_chapter_coverage_dedupes_and_filters_extra(self) -> None:
        data = {
            "outline_md": "x",
            "chapters": [
                {"number": 2, "title": "第二章", "beats": ["b"]},
                {"number": "2", "title": "第二章完整版", "beats": ["b1", "b2"]},
                {"number": 5, "title": "超出范围", "beats": ["overflow"]},
                {"number": "bad", "title": "无效", "beats": []},
            ],
        }
        out, warnings = _enforce_outline_chapter_coverage(data=data, target_chapter_count=3)
        chapters = out["chapters"]
        self.assertEqual([c["number"] for c in chapters], [2])
        self.assertEqual(chapters[0]["title"], "第二章完整版")
        self.assertIn("outline_chapter_number_deduped", warnings)
        self.assertIn("outline_chapter_invalid_filtered", warnings)
        self.assertIn("outline_chapter_beyond_target_filtered", warnings)
        self.assertIn("outline_chapter_coverage_incomplete", warnings)

    def test_enforce_outline_chapter_coverage_no_target_is_noop(self) -> None:
        data = {"outline_md": "x", "chapters": [{"number": 1, "title": "第一章", "beats": ["a"]}]}
        out, warnings = _enforce_outline_chapter_coverage(data=data, target_chapter_count=None)
        self.assertEqual(out["chapters"], data["chapters"])
        self.assertEqual(warnings, [])

    def test_format_chapter_number_ranges(self) -> None:
        self.assertEqual(_format_chapter_number_ranges([1, 2, 3, 7, 9, 10]), "1-3, 7, 9-10")
        self.assertEqual(_format_chapter_number_ranges([5]), "5")
        self.assertEqual(_format_chapter_number_ranges([]), "")

    def test_outline_fill_batch_size_is_adaptive(self) -> None:
        self.assertEqual(_outline_fill_batch_size_for_missing(0), 6)
        self.assertEqual(_outline_fill_batch_size_for_missing(9), 6)
        self.assertEqual(_outline_fill_batch_size_for_missing(10), 8)
        self.assertEqual(_outline_fill_batch_size_for_missing(24), 10)
        self.assertEqual(_outline_fill_batch_size_for_missing(50), 12)
        self.assertEqual(_outline_fill_batch_size_for_missing(90), 14)
        self.assertEqual(_outline_fill_batch_size_for_missing(200), 18)

    def test_outline_fill_max_attempts_scales_for_weak_models(self) -> None:
        # Missing 45 chapters must not be capped to a tiny fixed retry count.
        self.assertEqual(_outline_fill_max_attempts_for_missing(45), 11)
        # Missing 195 chapters should provide enough rounds for incremental completion.
        self.assertEqual(_outline_fill_max_attempts_for_missing(195), 41)
        self.assertEqual(_outline_fill_max_attempts_for_missing(0), 1)

    def test_outline_fill_progress_message(self) -> None:
        self.assertEqual(
            _outline_fill_progress_message({"attempt": 2, "max_attempts": 11, "remaining_count": 37}),
            "补全缺失章节... 第 2/11 轮，剩余 37 章",
        )
        self.assertEqual(
            _outline_fill_progress_message({"remaining_count": 9}),
            "补全缺失章节... 剩余 9 章",
        )
        self.assertEqual(_outline_fill_progress_message(None), "补全缺失章节...")

    def test_parse_outline_batch_output_uses_fallback_outline(self) -> None:
        text = json.dumps(
            {
                "chapters": [{"number": 1, "title": "第一章", "beats": ["事件 A"]}],
            },
            ensure_ascii=False,
        )
        data, warnings, parse_error = _parse_outline_batch_output(text=text, fallback_outline_md="# 已有总纲")
        self.assertIsNone(parse_error)
        self.assertEqual(data.get("outline_md"), "# 已有总纲")
        chapters = data.get("chapters") or []
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0]["number"], 1)
        self.assertEqual(warnings, [])

    def test_strip_segment_conflicting_prompt_sections(self) -> None:
        source = (
            "<REQUIREMENTS_JSON>{\"chapter_count\":500}</REQUIREMENTS_JSON>\n"
            "<CHAPTER_TARGET>\n目标章节数：500（请严格保证 chapters 数组条目数与之相同）\n</CHAPTER_TARGET>\n"
            "<STYLE_GUIDE>xx</STYLE_GUIDE>"
        )
        stripped = _strip_segment_conflicting_prompt_sections(source)
        self.assertNotIn("<CHAPTER_TARGET>", stripped)
        self.assertIn("<REQUIREMENTS_JSON>", stripped)
        self.assertIn("<STYLE_GUIDE>", stripped)

    def test_build_outline_segment_prompts_drops_chapter_target_block(self) -> None:
        base_user = (
            "<PROJECT>p</PROJECT>\n"
            "<CHAPTER_TARGET>\n目标章节数：120（请严格保证 chapters 数组条目数与之相同）\n</CHAPTER_TARGET>\n"
            "<REQUIREMENTS_JSON>{\"chapter_count\":120}</REQUIREMENTS_JSON>"
        )
        _system, user = _build_outline_segment_prompts(
            base_prompt_system="sys",
            base_prompt_user=base_user,
            target_chapter_count=120,
            batch_numbers=[1, 2, 3],
            existing_chapters=[],
            existing_outline_md="",
            attempt=1,
            max_attempts=3,
        )
        self.assertNotIn("<CHAPTER_TARGET>", user)
        self.assertIn("<SEGMENT_TASK>", user)
        self.assertIn("当前批次缺失章号", user)
        self.assertIn("当前批次章号数组", user)
        self.assertIn("已完成章号（禁止输出）", user)
        self.assertIn("输出前自检", user)

    def test_build_outline_segment_prompts_includes_previous_attempt_feedback(self) -> None:
        _system, user = _build_outline_segment_prompts(
            base_prompt_system="sys",
            base_prompt_user="<REQUIREMENTS_JSON>{\"chapter_count\":120}</REQUIREMENTS_JSON>",
            target_chapter_count=120,
            batch_numbers=[11, 12, 13],
            existing_chapters=[{"number": 1, "title": "第一章", "beats": ["a"]}],
            existing_outline_md="总纲",
            attempt=2,
            max_attempts=5,
            previous_output_numbers=[1, 2, 3],
            previous_failure_reason="输出章号与当前批次不匹配",
        )
        self.assertIn("<LAST_ATTEMPT_FEEDBACK>", user)
        self.assertIn("输出章号与当前批次不匹配", user)
        self.assertIn("上一轮输出章号：1-3", user)

    def test_segment_chapter_index_is_budgeted(self) -> None:
        chapters = [
            {"number": idx, "title": f"第{idx}章_" + ("标题" * 20), "beats": ["a", "b"]}
            for idx in range(1, 801)
        ]
        index_json = _build_outline_segment_chapter_index(chapters)
        self.assertLessEqual(len(index_json), OUTLINE_SEGMENT_INDEX_MAX_CHARS + 200)
        payload = json.loads(index_json)
        self.assertEqual(int(payload.get("total") or 0), 800)
        self.assertGreater(int(payload.get("omitted") or 0), 0)
        items = payload.get("items")
        self.assertIsInstance(items, list)
        self.assertLess(len(items), 800)

    def test_fill_prompt_uses_style_samples_and_adaptive_detail_rule(self) -> None:
        existing = [
            {"number": 1, "title": "第一章", "beats": ["a1", "a2", "a3", "a4"]},
            {"number": 2, "title": "第二章", "beats": ["b1", "b2", "b3", "b4"]},
            {"number": 3, "title": "第三章", "beats": ["c1", "c2", "c3", "c4"]},
            {"number": 4, "title": "第四章", "beats": ["d1", "d2", "d3", "d4"]},
        ]
        _system, user = _build_outline_missing_chapters_prompts(
            target_chapter_count=50,
            missing_numbers=[5, 6, 7],
            existing_chapters=existing,
            outline_md="x",
        )
        self.assertIn("风格参考样本", user)
        self.assertIn("中位数约 4 条", user)
        self.assertIn("本轮建议每章", user)

    def test_fill_missing_chapters_keeps_progressing_for_weak_model(self) -> None:
        llm_call = PreparedLlmCall(
            provider="openai",
            model="gpt-4o-mini",
            base_url="",
            timeout_seconds=180,
            params={"max_tokens": 12000},
            params_json=json.dumps({"max_tokens": 12000}, ensure_ascii=False),
            extra={},
        )
        data = {
            "outline_md": "x",
            "chapters": [{"number": i, "title": f"第{i}章", "beats": ["a"]} for i in range(1, 6)],
        }
        call_count = {"value": 0}
        progress_events: list[dict[str, object]] = []

        def _parse_missing_numbers(prompt_user: str) -> list[int]:
            m = re.search(r"缺失章号：([^\n]+)", prompt_user)
            if not m:
                return []
            text = m.group(1).strip()
            out: list[int] = []
            for token in [part.strip() for part in text.split(",") if part.strip()]:
                if "-" in token:
                    a, b = token.split("-", 1)
                    start = int(a.strip())
                    end = int(b.strip())
                    out.extend(range(start, end + 1))
                else:
                    out.append(int(token))
            return out

        def _fake_call_llm_and_record(**kwargs):  # type: ignore[no-untyped-def]
            call_count["value"] += 1
            prompt_user = str(kwargs.get("prompt_user") or "")
            missing = _parse_missing_numbers(prompt_user)
            # Simulate a weak model that only returns 5 chapters per call.
            selected = missing[:5]
            chapters = [{"number": n, "title": f"补全{n}", "beats": [f"事件{n}"]} for n in selected]
            text = json.dumps({"chapters": chapters}, ensure_ascii=False)
            return SimpleNamespace(text=text, finish_reason="stop", run_id=f"run-{call_count['value']}")

        with (
            patch("app.services.outline_generation.fill_service.call_llm_and_record", side_effect=_fake_call_llm_and_record),
            patch("app.services.outline_generation.gap_repair_service.call_llm_and_record", side_effect=_fake_call_llm_and_record),
            patch(
                "app.services.outline_generation.gap_repair_final_sweep_service.call_llm_and_record",
                side_effect=_fake_call_llm_and_record,
            ),
        ):
            out, warnings, _run_ids = _fill_outline_missing_chapters_with_llm(
                data=data,
                target_chapter_count=50,
                request_id="rid-test",
                actor_user_id="u1",
                project_id="p1",
                api_key="k",
                llm_call=llm_call,
                run_params_extra_json={},
                progress_hook=lambda update: progress_events.append(dict(update)),
            )

        chapters = out.get("chapters") or []
        self.assertEqual(len(chapters), 50)
        self.assertGreater(call_count["value"], 3)
        coverage = out.get("chapter_coverage") or {}
        self.assertEqual(coverage.get("missing_count"), 0)
        self.assertIn("outline_fill_missing_applied", warnings)
        applied = [e for e in progress_events if e.get("event") == "attempt_applied"]
        self.assertTrue(applied)
        latest = applied[-1]
        self.assertIsInstance(latest.get("chapters_snapshot"), list)
        self.assertEqual(int(latest.get("chapter_count") or 0), 50)
        self.assertGreater(int(latest.get("raw_output_chars") or 0), 0)
        self.assertGreater(len(str(latest.get("raw_output_preview") or "")), 0)

    def test_fill_missing_chapters_fail_soft_on_llm_error(self) -> None:
        llm_call = PreparedLlmCall(
            provider="openai",
            model="gpt-4o-mini",
            base_url="",
            timeout_seconds=180,
            params={"max_tokens": 12000},
            params_json=json.dumps({"max_tokens": 12000}, ensure_ascii=False),
            extra={},
        )
        data = {
            "outline_md": "x",
            "chapters": [{"number": i, "title": f"第{i}章", "beats": ["a"]} for i in range(1, 6)],
        }

        with (
            patch(
                "app.services.outline_generation.fill_service.call_llm_and_record",
                side_effect=AppError(code="LLM_TIMEOUT", message="timeout", status_code=504),
            ),
            patch(
                "app.services.outline_generation.gap_repair_service.call_llm_and_record",
                side_effect=AppError(code="LLM_TIMEOUT", message="timeout", status_code=504),
            ),
            patch(
                "app.services.outline_generation.gap_repair_final_sweep_service.call_llm_and_record",
                side_effect=AppError(code="LLM_TIMEOUT", message="timeout", status_code=504),
            ),
        ):
            out, warnings, run_ids = _fill_outline_missing_chapters_with_llm(
                data=data,
                target_chapter_count=50,
                request_id="rid-test",
                actor_user_id="u1",
                project_id="p1",
                api_key="k",
                llm_call=llm_call,
                run_params_extra_json={},
            )

        self.assertEqual(run_ids, [])
        self.assertIn("outline_fill_missing_call_failed", warnings)
        self.assertIn("outline_fill_missing_timeout", warnings)
        coverage = out.get("chapter_coverage") or {}
        self.assertGreater(int(coverage.get("missing_count") or 0), 0)

    def test_fill_missing_chapters_final_sweep_repairs_remaining_gaps(self) -> None:
        llm_call = PreparedLlmCall(
            provider="openai",
            model="gpt-4o-mini",
            base_url="",
            timeout_seconds=180,
            params={"max_tokens": 12000},
            params_json=json.dumps({"max_tokens": 12000}, ensure_ascii=False),
            extra={},
        )
        data = {
            "outline_md": "x",
            "chapters": [{"number": i, "title": f"第{i}章", "beats": ["a"]} for i in range(1, 11)],
        }
        call_count = {"value": 0}
        progress_events: list[dict[str, object]] = []

        def _parse_missing_json(prompt_user: str) -> list[int]:
            m = re.search(r"缺失章号数组（严格按此输出）：(\[[^\n]+\])", prompt_user)
            if not m:
                return []
            try:
                raw = json.loads(m.group(1))
            except Exception:
                return []
            out: list[int] = []
            if isinstance(raw, list):
                for item in raw:
                    try:
                        value = int(item)
                    except Exception:
                        continue
                    if value > 0:
                        out.append(value)
            return out

        def _fake_call_llm_and_record(**kwargs):  # type: ignore[no-untyped-def]
            call_count["value"] += 1
            run_type = str(kwargs.get("run_type") or "")
            prompt_user = str(kwargs.get("prompt_user") or "")
            missing = _parse_missing_json(prompt_user)
            if run_type in ("outline_fill_missing", "outline_gap_repair"):
                # Force regular补全阶段停滞，触发终检兜底。
                text = json.dumps({"chapters": [{"number": 1, "title": "重复章节", "beats": ["x"]}]}, ensure_ascii=False)
            elif run_type == "outline_gap_repair_final_sweep":
                number = missing[0] if missing else 0
                chapter = {"number": number, "title": f"兜底补全{number}", "beats": [f"事件{number}"]} if number > 0 else {}
                text = json.dumps({"chapters": [chapter] if chapter else []}, ensure_ascii=False)
            else:
                raise AssertionError(f"unexpected run_type: {run_type}")
            return SimpleNamespace(text=text, finish_reason="stop", run_id=f"run-{call_count['value']}")

        with (
            patch("app.services.outline_generation.fill_service.call_llm_and_record", side_effect=_fake_call_llm_and_record),
            patch("app.services.outline_generation.gap_repair_service.call_llm_and_record", side_effect=_fake_call_llm_and_record),
            patch(
                "app.services.outline_generation.gap_repair_final_sweep_service.call_llm_and_record",
                side_effect=_fake_call_llm_and_record,
            ),
        ):
            out, warnings, run_ids = _fill_outline_missing_chapters_with_llm(
                data=data,
                target_chapter_count=20,
                request_id="rid-test",
                actor_user_id="u1",
                project_id="p1",
                api_key="k",
                llm_call=llm_call,
                run_params_extra_json={},
                progress_hook=lambda update: progress_events.append(dict(update)),
            )

        chapters = out.get("chapters") or []
        self.assertEqual(len(chapters), 20)
        self.assertEqual([int(chapters[0]["number"]), int(chapters[-1]["number"])], [1, 20])
        coverage = out.get("chapter_coverage") or {}
        self.assertEqual(int(coverage.get("missing_count") or 0), 0)
        self.assertIn("outline_gap_repair_final_sweep_applied", warnings)
        self.assertIn("outline_gap_repair_final_sweep_resolved", warnings)
        self.assertTrue(any("outline_gap_repair_final_sweep" in rid for rid in run_ids) or len(run_ids) > 0)
        applied_events = [e for e in progress_events if e.get("event") == "gap_repair_final_sweep_applied"]
        self.assertTrue(applied_events)

    def test_segmented_generation_recovers_sparse_batch_outputs(self) -> None:
        llm_call = PreparedLlmCall(
            provider="openai",
            model="gpt-4o-mini",
            base_url="",
            timeout_seconds=180,
            params={"max_tokens": 4096},
            params_json=json.dumps({"max_tokens": 4096}, ensure_ascii=False),
            extra={},
        )
        call_count = {"value": 0}
        progress_events: list[dict[str, object]] = []

        def _fake_call_llm_and_record(**kwargs):  # type: ignore[no-untyped-def]
            call_count["value"] += 1
            extra = kwargs.get("run_params_extra_json")
            segment_meta = extra.get("outline_segment") if isinstance(extra, dict) else {}
            batch_numbers_raw = segment_meta.get("batch_numbers") if isinstance(segment_meta, dict) else []
            batch_numbers = [int(n) for n in batch_numbers_raw] if isinstance(batch_numbers_raw, list) else []
            selected = batch_numbers[:5]
            chapters = [{"number": n, "title": f"第{n}章", "beats": [f"事件{n}"]} for n in selected]
            outline_md = "# 分段总纲\n\n- 用于测试分段收敛" if int(segment_meta.get("batch_index") or 0) == 1 else ""
            text = json.dumps({"outline_md": outline_md, "chapters": chapters}, ensure_ascii=False)
            return SimpleNamespace(
                text=text,
                finish_reason="stop",
                run_id=f"run-{call_count['value']}",
                latency_ms=35,
                dropped_params=[],
            )

        with patch("app.services.outline_generation.segment_service.call_llm_and_record", side_effect=_fake_call_llm_and_record):
            res = _generate_outline_segmented_with_llm(
                request_id="rid-segment-test",
                actor_user_id="u1",
                project_id="p1",
                api_key="k",
                llm_call=llm_call,
                prompt_system="sys",
                prompt_user="usr",
                target_chapter_count=26,
                run_params_extra_json={},
                progress_hook=lambda update: progress_events.append(dict(update)),
            )

        chapters = res.data.get("chapters") or []
        self.assertIsNone(res.parse_error)
        self.assertEqual(len(chapters), 26)
        self.assertEqual(chapters[0]["number"], 1)
        self.assertEqual(chapters[-1]["number"], 26)
        self.assertGreater(call_count["value"], 5)
        self.assertIn("outline_segment_applied", res.warnings)
        applied_events = [evt for evt in progress_events if evt.get("event") == "batch_applied"]
        self.assertTrue(applied_events)
        self.assertGreater(int(applied_events[0].get("raw_output_chars") or 0), 0)
        self.assertGreater(len(str(applied_events[0].get("raw_output_preview") or "")), 0)


if __name__ == "__main__":
    unittest.main()
