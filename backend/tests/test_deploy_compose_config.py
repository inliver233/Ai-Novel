"""默认部署配置不变量检查（H44 known_issue）。

H44（项目情况完全分析.md）：``docker-compose.yml`` 默认 postgres 镜像为
``postgres:16-alpine``，**不含 pgvector 扩展** → pgvector 迁移 fail-soft 跳过、
``vector_chunks`` 表永不创建、revision 却标记已应用；同时 ``chromadb`` 不在任何
requirements 文件里 → 默认部署两个向量后端都静默失效，而 README 把向量 RAG 当核心
特性宣传。本测试断言【正确行为】：开箱即用的默认部署应让向量检索可用——postgres
镜像需支持 pgvector（镜像名含 ``pgvector``），且默认安装清单应含 ``chromadb`` 依赖。
当前实现两者皆缺，标 ``known_issue``，测试真跑真红以暴露现存 backlog。

说明：被测对象是部署配置文件的**契约**（默认部署应提供可用的向量后端），断言的是
配置内容而非运行时行为，故纯文件读取即可，不需 DB 脚手架。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = _REPO_ROOT / "docker-compose.yml"
_BACKEND_DIR = Path(__file__).resolve().parents[1]
# Dockerfile 执行 `pip install -r requirements.lock.txt`，即默认部署安装清单。
_DEFAULT_REQUIREMENTS = _BACKEND_DIR / "requirements.lock.txt"


# H44: 默认 postgres 镜像必须支持 pgvector，否则向量检索核心功能在开箱即用部署中不可用
@pytest.mark.known_issue
def test_default_postgres_image_supports_pgvector() -> None:
    compose = yaml.safe_load(_COMPOSE_FILE.read_text(encoding="utf-8"))
    image = compose["services"]["postgres"]["image"]
    # 正确行为：镜像名应含 "pgvector"（如 pgvector/pgvector:pg16）。
    # 当前 bug：postgres:16-alpine 不含 pgvector 扩展 → pgvector 迁移静默跳过。
    assert "pgvector" in image.lower(), (
        f"默认 postgres 镜像 {image!r} 不含 pgvector 扩展，"
        "开箱即用部署无法创建 vector_chunks 表，向量 RAG 核心功能静默失效"
    )


# H44: 默认部署安装清单应含 chromadb 依赖，否则 chroma 向量后端在默认部署中不可用
@pytest.mark.known_issue
def test_default_deploy_includes_chromadb_dependency() -> None:
    text = _DEFAULT_REQUIREMENTS.read_text(encoding="utf-8")
    names: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 取包名首段（剥离版本/标记/选项），如 "chromadb==0.5.0" → "chromadb"
        name = re.split(r"[\s<>=!~;\[]", line, maxsplit=1)[0]
        if name:
            names.add(name.lower())
    # 正确行为：默认安装清单应声明 chromadb。
    # 当前 bug：requirements.lock.txt 无 chromadb → chroma 后端在默认部署中静默失效。
    assert "chromadb" in names, (
        "默认部署安装清单 requirements.lock.txt 未声明 chromadb 依赖，"
        "chroma 向量后端在开箱即用部署中不可用"
    )
