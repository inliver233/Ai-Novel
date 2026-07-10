from __future__ import annotations

import unittest
from typing import cast

from app.services.output_contracts import (
    OutputContract,
    OutputContractType,
    build_repair_prompt_for_task,
    contract_for_task,
)


class TestContentOptimizeContract(unittest.TestCase):
    def test_parses_content_tag_block(self) -> None:
        contract = contract_for_task("content_optimize")
        parsed = contract.parse("<content>hello</content>")
        self.assertEqual(parsed.parse_error, None)
        self.assertEqual(parsed.data.get("content_md"), "hello")

    def test_allows_content_tag_with_attributes(self) -> None:
        contract = contract_for_task("content_optimize")
        parsed = contract.parse('<content data-x="1">hello</content>')
        self.assertEqual(parsed.parse_error, None)
        self.assertEqual(parsed.data.get("content_md"), "hello")

    def test_task_catalog_maps_every_supported_contract(self) -> None:
        self.assertEqual(contract_for_task("outline_generate").type, "json")
        self.assertEqual(contract_for_task("chapter_generate").type, "markers")
        self.assertEqual(
            (contract_for_task("plan_chapter").type, contract_for_task("plan_chapter").tag),
            ("tags", "plan"),
        )
        self.assertEqual(contract_for_task("post_edit").output_key, "content_md")
        self.assertEqual(contract_for_task("content_optimize").tag, "content")
        self.assertEqual(contract_for_task("unknown_task").type, "markers")

    def test_marker_contract_returns_a_structured_result(self) -> None:
        parsed = contract_for_task("chapter_generate").parse("plain model output")

        self.assertIsInstance(parsed.data, dict)
        self.assertIsInstance(parsed.warnings, list)

    def test_outline_length_finish_reason_adds_truncation_diagnostics(self) -> None:
        parsed = contract_for_task("outline_generate").parse("{", finish_reason="length")

        self.assertIn("output_truncated", parsed.warnings)
        self.assertIsNotNone(parsed.parse_error)
        self.assertTrue(str((parsed.parse_error or {}).get("hint") or ""))

    def test_invalid_contract_configuration_returns_structured_errors(self) -> None:
        missing_tag = OutputContract(type="tags", output_key="value").parse("raw")
        self.assertEqual(missing_tag.data, {"value": "", "raw_output": "raw"})
        self.assertEqual((missing_tag.parse_error or {}).get("code"), "TAG_PARSE_ERROR")

        unknown = OutputContract(type=cast(OutputContractType, "unknown")).parse("raw")
        self.assertEqual(unknown.data, {"raw_output": "raw"})
        self.assertEqual((unknown.parse_error or {}).get("code"), "OUTPUT_CONTRACT_ERROR")

    def test_repair_prompt_is_available_only_for_outline_generation(self) -> None:
        repair = build_repair_prompt_for_task("outline_generate", raw_output="raw-output-sentinel")

        self.assertIsNotNone(repair)
        system, user, run_type = repair or ("", "", "")
        self.assertTrue(system)
        self.assertIn("raw-output-sentinel", user)
        self.assertEqual(run_type, "outline_fix_json")
        self.assertIsNone(build_repair_prompt_for_task("chapter_generate", raw_output="raw"))


if __name__ == "__main__":
    unittest.main()
