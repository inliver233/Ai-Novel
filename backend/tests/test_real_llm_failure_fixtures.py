from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.schemas.characters_auto_update import CharactersAutoUpdateV1Request
from app.services.output_parsers import extract_json_value


FIX_DIR = Path(__file__).parent / "fixtures" / "real_llm_failures"


class TestRealLlmFailureFixtures(unittest.TestCase):
    def test_fixtures_are_present(self) -> None:
        self.assertTrue(FIX_DIR.exists())
        index_path = FIX_DIR / "index.json"
        self.assertTrue(index_path.exists())

        items = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(items), 5)
        for item in items:
            self.assertTrue((FIX_DIR / item["file"]).exists(), msg=item)

    def test_fixtures_sanitized_no_api_keys(self) -> None:
        for path in FIX_DIR.glob("*"):
            if path.is_dir():
                continue
            raw = path.read_text(encoding="utf-8", errors="replace")
            self.assertNotRegex(raw, r"sk-[A-Za-z0-9]{10,}", msg=str(path))
            lowered = raw.lower()
            self.assertNotIn("api_key", lowered, msg=str(path))
            self.assertNotIn("authorization:", lowered, msg=str(path))

    def test_characters_auto_update_fixture_reproduces_schema_drift(self) -> None:
        p = FIX_DIR / "b497cd7a-ed94-4171-8392-8e7f28773e48.characters_auto_update.output.txt"
        text = p.read_text(encoding="utf-8")
        value, _raw_json = extract_json_value(text)

        self.assertIsInstance(value, dict)
        self.assertEqual(value.get("schema_version"), "characters_auto_update_v1")
        self.assertIsInstance(value.get("ops"), list)
        self.assertIn("character", value["ops"][0])

        parsed = CharactersAutoUpdateV1Request.model_validate(value)
        self.assertEqual(parsed.ops[0].op, "upsert")
        self.assertEqual(parsed.ops[0].name, "光头强")
        self.assertIsInstance(parsed.ops[0].patch, dict)

