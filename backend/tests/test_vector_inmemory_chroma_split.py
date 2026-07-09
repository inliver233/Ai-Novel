"""chromadb 缺失时内存兜底"写读分家" (rag §4.2 known_issue)。

rag §4.2（项目情况完全分析.md）：当 chromadb 未安装时，in-memory 兜底在**两个模块**
各自维护了一套独立存储：
- 写路径：``vector_storage.ingest_chunks`` -> ``vector_storage._get_collection``
  -> ``vector_storage._import_chromadb`` -> ``vector_storage._INMEMORY_CHROMA``。
- 读路径：``vector_retrieval.query_project`` -> 从 ``vector_build`` import 的
  ``_get_collection`` -> ``vector_build._import_chromadb`` ->
  ``vector_build._INMEMORY_CHROMA``。

两套 ``_INMEMORY_CHROMA`` 是不同 dict 对象。故 ingest 写入的 chunk 落在写存储里，
而 query 从读存储（空的）取 -> 返回 0 候选且**不报错**。后果：sqlite/dev 无 chromadb
环境下 RAG 检索**恒为空**。

本测试断言【正确行为】：经生产写路径 ingest 后，经生产读路径 query 必须能取回刚写入的
候选（``len(result["candidates"]) > 0``）。当前实现有 bug，标 ``known_issue``。

确定性说明：测试不依赖 chromadb 是否真实安装——用 monkeypatch 把**两个模块各自的**
``_import_chromadb`` 强制指向**各自真实的** ``_INMEMORY_CHROMADB``，迫使其走真实的
内存兜底分支。两个真实 dict 之间本就互不相通，这正是 bug 本身，而非 mock 伪象。
（若临时把两个 dict 指向同一对象，测试即转绿——可证明失败确因"写读分家"。）
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import vector_build, vector_rag_service, vector_retrieval, vector_storage
from app.services.vector_build import VectorChunk
from app.services.vector_retrieval import query_project
from app.services.vector_storage import ingest_chunks


def _force_inmemory_storage() -> Any:
    """强制写路径走 vector_storage 自己的内存兜底（chromadb 缺失时的真实分支）。"""
    return vector_storage._INMEMORY_CHROMADB


def _force_inmemory_build() -> Any:
    """强制读路径走 vector_build 自己的内存兜底（chromadb 缺失时的真实分支）。"""
    return vector_build._INMEMORY_CHROMADB


def _fake_embed(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """跳过真实 embedding：写/读返回同一向量 -> 余弦距离=0 -> 必命中。"""
    return {"enabled": True, "vectors": [[1.0, 0.0, 0.0]]}


def _enabled_ok(*_args: Any, **_kwargs: Any) -> tuple[bool, None]:
    """让 enabled 检查恒通过，不依赖真实 embedding 配置。"""
    return (True, None)


# RAG §4.2: chromadb 缺失时内存兜底写读分家(vector_storage 写 / vector_build 读)致检索恒空
@pytest.mark.known_issue
def test_inmemory_chroma_write_and_read_share_store(monkeypatch: pytest.MonkeyPatch) -> None:
    # --- 强制两个模块各自走真实的内存兜底分支 ---
    monkeypatch.setattr(vector_storage, "_import_chromadb", _force_inmemory_storage)
    monkeypatch.setattr(vector_build, "_import_chromadb", _force_inmemory_build)

    # --- 跳过真实 embedding（写/读都用同一向量，保证命中） ---
    monkeypatch.setattr(vector_storage, "embed_texts_with_providers", _fake_embed)
    monkeypatch.setattr(vector_retrieval, "embed_texts_with_providers", _fake_embed)

    # --- 让 enabled 检查在所有消费点通过 ---
    # ingest_chunks 经 `_hub`(=vector_rag_service) 取 _vector_enabled_reason；
    # query_project 用本模块 import 的 _vector_enabled_reason。三处都打掉以确定。
    monkeypatch.setattr(vector_build, "_vector_enabled_reason", _enabled_ok)
    monkeypatch.setattr(vector_rag_service, "_vector_enabled_reason", _enabled_ok)
    monkeypatch.setattr(vector_retrieval, "_vector_enabled_reason", _enabled_ok)

    # --- 隔离：清空两个模块的内存存储 ---
    vector_storage._INMEMORY_CHROMA.clear()
    vector_build._INMEMORY_CHROMA.clear()

    project_id = "rag-4-2-split"
    chunk = VectorChunk(
        id="c1",
        text="hello world dragon",
        metadata={"source": "chapter", "source_id": "c1", "chunk_index": 0},
    )

    # 生产写路径
    ingest_out = ingest_chunks(project_id=project_id, kb_id=None, chunks=[chunk])
    assert ingest_out.get("enabled") is True, ingest_out
    assert ingest_out.get("ingested") == 1, ingest_out

    # 生产读路径
    result = query_project(
        project_id=project_id,
        kb_id=None,
        query_text="hello world dragon",
        sources=["chapter"],
    )

    # 正确行为：刚写入的 chunk 应能被检索到（candidates 非空）。
    # 当前 bug：写读分家 -> 读的是另一个空存储 -> candidates 恒为 []。
    assert len(result["candidates"]) > 0, result.get("candidates")
