"""向量 source_order 配置解析（H14 / rag-4.5 known_issue）。

H14（项目情况完全分析.md rag-4.5）：``_parse_vector_source_order`` 与
``_super_sort_final_chunks`` 用的正则 ``r"[\\s,|;]+"`` 在 raw 字符串里 ``\\`` 是
字面"反斜杠 + s"，故字符类 ``[\\s,|;]`` 实际匹配 {反斜杠, 字母 s, 逗号, 竖线,
分号}——**不匹配空白**，且会把源名里的字母 ``s`` 当分隔符。后果：解析
``"chapter,outline,story_memory"`` 时 ``story_memory`` 的首字母 ``s`` 被当作分隔符，
切成 ``""`` + ``"tory_memory"``，二者均非合法源被过滤 → **story_memory 源丢失**，
source_order 配置静默失效。本测试断言【正确行为】：逗号分隔的合法源列表应解析为
全部源；当前实现有 bug，标 ``known_issue``。

说明：被测对象是配置解析器的**契约**（输入合法源串 → 输出该源列表），断言的是输出
而非正则实现，故重构解析器不会误伤。bug 根源在此私有 parser，直接测它是暴露该 bug
在 sqlite 环境下的唯一确定路径（经完整检索路径需 pgvector）。正解应同时修复 :230
与 :283 两处（或抽共享 helper）。
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.services.vector_retrieval import _parse_vector_source_order


# H14: source_order 正则误把字母 s 当分隔符 → story_memory 源丢失
@pytest.mark.known_issue
def test_source_order_parse_keeps_s_sources() -> None:
    original = getattr(settings, "vector_source_order", "")
    settings.vector_source_order = "chapter,outline,story_memory"
    try:
        result = _parse_vector_source_order()
    finally:
        settings.vector_source_order = original
    # 正确行为：三个合法源都应保留（含首字母为 s 的 story_memory）。
    # 当前 bug：story_memory 被正则按 's' 切碎后过滤掉 → 只剩 chapter,outline。
    assert result == ["chapter", "outline", "story_memory"]


# H14: source_order 支持空白/竖线/分号作分隔符（正则本意），当前只按逗号+s 工作
@pytest.mark.known_issue
def test_source_order_parse_supports_whitespace_separator() -> None:
    original = getattr(settings, "vector_source_order", "")
    settings.vector_source_order = "chapter outline story_memory"
    try:
        result = _parse_vector_source_order()
    finally:
        settings.vector_source_order = original
    # 正确行为：空白分隔也应正确解析（正则 \s 本意即此）。
    # 当前 bug：正则不匹配空白 → 整串当一个源 "chapter outline story_memory" → 非法 → None。
    assert result == ["chapter", "outline", "story_memory"]
