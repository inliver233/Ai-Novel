from __future__ import annotations

from pathlib import Path
import unittest

from app.api.routes.outline import (
    _build_outline_generation_guidance,
    _extract_target_chapter_count,
    _recommend_outline_max_tokens,
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
        self.assertIn("1~2", guidance["chapter_detail_rule"])

    def test_build_outline_generation_guidance_default(self) -> None:
        guidance = _build_outline_generation_guidance(None)
        self.assertEqual(guidance["chapter_count_rule"], "")
        self.assertIn("5~9", guidance["chapter_detail_rule"])

    def test_recommend_outline_max_tokens(self) -> None:
        # gpt-4o-mini output limit is 16384; 200 chapters should recommend 12000 when current max is lower.
        self.assertEqual(
            _recommend_outline_max_tokens(
                target_chapter_count=200,
                provider="openai",
                model="gpt-4o-mini",
                current_max_tokens=4096,
            ),
            12000,
        )
        # gpt-4 output limit is 8192; recommendation should be clamped.
        self.assertEqual(
            _recommend_outline_max_tokens(
                target_chapter_count=200,
                provider="openai",
                model="gpt-4",
                current_max_tokens=4096,
            ),
            8192,
        )
        # If current max_tokens is already high enough, no override is needed.
        self.assertIsNone(
            _recommend_outline_max_tokens(
                target_chapter_count=200,
                provider="openai",
                model="gpt-4o-mini",
                current_max_tokens=12000,
            )
        )
        # Small chapter count should not override.
        self.assertIsNone(
            _recommend_outline_max_tokens(
                target_chapter_count=20,
                provider="openai",
                model="gpt-4o-mini",
                current_max_tokens=4096,
            )
        )

    def test_outline_contract_template_uses_dynamic_rules(self) -> None:
        template_path = Path("app/resources/prompt_presets/outline_generate_v3/templates/sys.outline.contract.json.md")
        template = template_path.read_text(encoding="utf-8")

        rendered, _missing, error = render_template(
            template,
            values={
                "chapter_count_rule": "chapters 必须输出 200 章，number 需完整覆盖 1..200 且不缺号。",
                "chapter_detail_rule": "beats 每章 1~2 条，极简表达关键推进；若长度受限，优先保留章节覆盖与编号完整。",
            },
            macro_seed="test-seed",
        )
        self.assertIsNone(error)
        self.assertIn("200 章", rendered)
        self.assertIn("1~2 条", rendered)

        rendered_default, _missing_default, error_default = render_template(template, values={}, macro_seed="test-seed")
        self.assertIsNone(error_default)
        self.assertIn("beats 每章 5~9 条", rendered_default)


if __name__ == "__main__":
    unittest.main()
