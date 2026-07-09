"""prompt_budget 纯函数测试（H41/M23 缺口：此前零直接测试）。

estimate_tokens 是 RAG/生成预算裁剪的基础，行为正确性影响全链路 token 上限。
"""
from __future__ import annotations

from app.services.prompt_budget import estimate_tokens, trim_text_to_tokens


class TestEstimateTokens:
    def test_empty_is_zero(self) -> None:
        assert estimate_tokens("") == 0

    def test_cjk_is_one_token_per_char(self) -> None:
        # CJK 基本区每个字约 1 token。
        assert estimate_tokens("你好世界") == 4

    def test_non_cjk_is_about_four_chars_per_token(self) -> None:
        assert estimate_tokens("abcd") == 1          # 4 chars → ceil(4/4)=1
        assert estimate_tokens("abcde") == 2         # 5 chars → ceil(5/4)=2
        assert estimate_tokens("abcdefgh") == 2      # 8 chars → 2

    def test_mixed(self) -> None:
        # 2 CJK + 4 ASCII = 2 + ceil(4/4) = 3
        assert estimate_tokens("你好abcd") == 3


class TestTrimTextToTokens:
    def test_zero_or_negative_returns_empty(self) -> None:
        assert trim_text_to_tokens("anything", 0) == ""
        assert trim_text_to_tokens("anything", -5) == ""

    def test_empty_returns_empty(self) -> None:
        assert trim_text_to_tokens("", 100) == ""

    def test_under_limit_returns_full(self) -> None:
        text = "你好世界"           # 4 tokens
        assert trim_text_to_tokens(text, 10) == text

    def test_over_limit_trims_within_budget(self) -> None:
        text = "你好世界你好世界"    # 8 tokens
        trimmed = trim_text_to_tokens(text, 5)
        # 裁剪后既不能超过预算，也应是原文前缀。
        assert trimmed == text[: len(trimmed)]
        assert estimate_tokens(trimmed) <= 5
        assert estimate_tokens(trimmed) > 0

    def test_trim_is_maximal(self) -> None:
        # 二分搜索应给出不超过预算的最大前缀。
        text = "a" * 40              # 10 tokens
        trimmed = trim_text_to_tokens(text, 3)
        assert estimate_tokens(trimmed) == 3          # 3 tokens = 12 chars
        assert len(trimmed) == 12
