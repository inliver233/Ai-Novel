"""llm_task_catalog 纯函数测试（H41/M28 缺口：双任务目录不同步风险）。

锁定：is_supported_llm_task / llm_task_label 行为，以及目录自身完整性
（键唯一、无空标签），为后续统一任务目录的重构提供回归基线。
"""
from __future__ import annotations

from app.services.llm_task_catalog import (
    LLM_TASK_CATALOG,
    LLM_TASK_KEY_SET,
    is_supported_llm_task,
    llm_task_label,
)


class TestCatalogIntegrity:
    def test_keys_unique(self) -> None:
        keys = [item.key for item in LLM_TASK_CATALOG]
        assert len(keys) == len(set(keys))

    def test_no_empty_label_or_key(self) -> None:
        for item in LLM_TASK_CATALOG:
            assert item.key.strip(), item
            assert item.label.strip(), item
            assert item.group.strip(), item

    def test_key_set_matches_catalog(self) -> None:
        assert LLM_TASK_KEY_SET == frozenset(item.key for item in LLM_TASK_CATALOG)


class TestIsSupportedLlmTask:
    def test_known_keys_supported(self) -> None:
        assert is_supported_llm_task("chapter_generate") is True
        assert is_supported_llm_task("outline_generate") is True

    def test_unknown_key_unsupported(self) -> None:
        assert is_supported_llm_task("not_a_real_task") is False

    def test_empty_unsupported(self) -> None:
        assert is_supported_llm_task("") is False
        assert is_supported_llm_task("   ") is False


class TestLlmTaskLabel:
    def test_known_key_returns_label(self) -> None:
        assert llm_task_label("chapter_generate") == "章节生成"

    def test_unknown_key_echoes_input(self) -> None:
        assert llm_task_label("mystery_task") == "mystery_task"

    def test_empty_returns_empty(self) -> None:
        assert llm_task_label("") == ""
