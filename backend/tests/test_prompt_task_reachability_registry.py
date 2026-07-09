from __future__ import annotations

import unittest
from pathlib import Path

from app.services.prompt_task_catalog import PROMPT_TASK_CATALOG, PROMPT_TASK_KEYS


class TestPromptTaskReachabilityRegistry(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.frontend_catalog_path = cls.repo_root / "frontend" / "src" / "lib" / "promptTaskCatalog.ts"
        cls.ui_copy_path = cls.repo_root / "frontend" / "src" / "lib" / "uiCopy.ts"

    def test_backend_catalog_keys_unique(self) -> None:
        self.assertGreater(len(PROMPT_TASK_KEYS), 0)
        self.assertEqual(len(PROMPT_TASK_KEYS), len(set(PROMPT_TASK_KEYS)))

    def test_frontend_prompt_task_catalog_covers_backend_tasks(self) -> None:
        text = self.frontend_catalog_path.read_text(encoding="utf-8")
        for task in PROMPT_TASK_CATALOG:
            self.assertIn(f'key: "{task.key}"', text)

    def test_ui_copy_registry_covers_backend_tasks(self) -> None:
        # 跨层一致性：前端 uiCopy.ts 必须为每个后端 prompt 任务提供文案键。
        # （原版本还断言 Playwright e2e spec 文件存在，但 e2e 基建已在 lite 裁剪中
        # 删除——见 项目情况完全分析.md C9，故此处只保留仍有效的 ui_copy 检查。）
        ui_copy_text = self.ui_copy_path.read_text(encoding="utf-8")
        for task in PROMPT_TASK_CATALOG:
            self.assertIn(f"{task.ui_copy_key}:", ui_copy_text)


if __name__ == "__main__":
    unittest.main()
