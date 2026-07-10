"""默认部署配置不变量检查（H44 / deploy#1 safety）。

H44（项目情况完全分析.md）：``docker-compose.yml`` 默认 postgres 镜像为
``postgres:16-alpine``，**不含 pgvector 扩展** → pgvector 迁移 fail-soft 跳过、
``vector_chunks`` 表永不创建、revision 却标记已应用；同时 ``chromadb`` 不在任何
requirements 文件里 → 默认部署两个向量后端都静默失效，而 README 把向量 RAG 当核心
特性宣传。本测试断言【正确行为】：开箱即用的默认部署至少提供一个可用向量后端——
postgres 镜像支持 pgvector，**或**默认安装清单包含 ``chromadb``。这是产品能力的 OR
契约；不强迫部署同时安装两个后端。

说明：被测对象是部署配置文件的**契约**（默认部署应提供可用的向量后端），断言的是
配置内容而非运行时行为，故纯文件读取即可，不需 DB 脚手架。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = _REPO_ROOT / "docker-compose.yml"
_BACKEND_DIR = Path(__file__).resolve().parents[1]
# Dockerfile 执行 `pip install -r requirements.lock.txt`，即默认部署安装清单。
_DEFAULT_REQUIREMENTS = _BACKEND_DIR / "requirements.lock.txt"


# H44/deploy#1: 默认部署至少提供 pgvector 或 Chroma 中的一条可用路径
def test_default_deploy_provides_at_least_one_vector_backend() -> None:
    compose = yaml.safe_load(_COMPOSE_FILE.read_text(encoding="utf-8"))
    image = compose["services"]["postgres"]["image"]
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

    has_pgvector = "pgvector" in str(image).lower()
    has_chromadb = "chromadb" in names

    # 正确行为：至少一条持久化向量后端可用；只修任一条路径即可满足部署契约。
    assert has_pgvector or has_chromadb, (
        f"默认部署没有可用向量后端：postgres image={image!r}, "
        f"chromadb_dependency={has_chromadb}"
    )
