"""向量检索 source 列表一致性（M13 known_issue）。

M13（项目情况完全分析.md）：``_ALL_SOURCES`` 在三处定义不一致——
``vector_build.py:44`` 含 lite 死源 ``worldbook``（4 源），而
``vector_chunk_builder.py:12`` 与 ``vector_rag_service.py:22`` 不含 ``worldbook``
（3 源）。``vector_storage`` / ``vector_retrieval`` 经 ``vector_rag_service``
re-export，用的是 3 源版本。于是 build（写入）路径与 query（读取）路径的 source
集合不一致——build 会为死源 worldbook 构建 chunk，而 query 的 overfilter relax
以 3 源为全集，逻辑分歧。本测试断言【正确行为】：三处应一致；当前实现有 bug，
标 ``known_issue``。

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
    # 正确行为：三处 _ALL_SOURCES 应完全一致（同一套可用 source）。
    # 当前 bug：vector_build 含 worldbook（lite 死源），其余两处不含 → 不等。
    assert BUILD_SOURCES == RAG_SERVICE_SOURCES == CHUNK_BUILDER_SOURCES
