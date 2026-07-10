"""向量检索 source 列表一致性（M13 known_issue）。

M13（项目情况完全分析.md）：``_ALL_SOURCES`` 在三处定义不一致——
``vector_build.py:44`` 含 lite 死源 ``worldbook``（4 源），而
``vector_chunk_builder.py:12`` 与 ``vector_rag_service.py:22`` 不含 ``worldbook``
（3 源）。不同运行路径分别导入这些副本，导致默认查询、构建与 overfilter relax
对“全集”的理解不一致。本测试断言【正确行为】：三处都必须等于 lite 的规范三源
``outline/chapter/story_memory``；当前 build 副本仍含死源 worldbook，故标
``known_issue``。

说明：此处引用带下划线的模块级常量，是因为 M13 的 bug 根源就是这三个常量彼此
不一致——要测该 bug 必须比较它们。这不属于"深挖路由私有函数"的范畴（被测对象
是数据常量，而非实现细节）。
"""

from __future__ import annotations

import pytest

from app.services.vector_build import _ALL_SOURCES as BUILD_SOURCES
from app.services.vector_chunk_builder import _ALL_SOURCES as CHUNK_BUILDER_SOURCES
from app.services.vector_rag_service import _ALL_SOURCES as RAG_SERVICE_SOURCES


# M13: _ALL_SOURCES 三处定义不一致（vector_build 含死源 worldbook）
@pytest.mark.known_issue
def test_all_sources_consistent_across_modules() -> None:
    # 正确行为：三处既要一致，也必须等于 lite 的规范活源集合。仅断言“相等”会
    # 允许把死源 worldbook 错误地加回三处后假毕业。
    canonical_sources = ["outline", "chapter", "story_memory"]
    assert BUILD_SOURCES == canonical_sources
    assert RAG_SERVICE_SOURCES == canonical_sources
    assert CHUNK_BUILDER_SOURCES == canonical_sources
