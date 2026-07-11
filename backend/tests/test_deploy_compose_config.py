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
_NGINX_CONFIG = _REPO_ROOT / "frontend" / "nginx.conf"
_ENTRYPOINT = _BACKEND_DIR / "scripts" / "entrypoint.sh"


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


def _env_values(path: Path) -> dict[str, str]:
    return {
        line.partition("=")[0].strip(): line.partition("=")[2].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }


def test_auth_rate_limit_deploy_proxy_contract_is_explicit_and_consistent() -> None:
    compose = yaml.safe_load(_COMPOSE_FILE.read_text(encoding="utf-8"))
    environment = compose["x-backend-environment"]
    frontend_ip = compose["services"]["frontend"]["networks"]["frontend_proxy"]["ipv4_address"]
    assert frontend_ip == "172.31.250.10"
    assert "frontend_proxy" in compose["services"]["backend"]["networks"]
    assert compose["networks"]["frontend_proxy"]["ipam"]["config"][0]["subnet"] == "172.31.250.0/24"
    assert environment["AUTH_TRUSTED_PROXY_CIDRS"].endswith("172.31.250.10/32}")

    nginx = _NGINX_CONFIG.read_text(encoding="utf-8")
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in nginx
    assert "$proxy_add_x_forwarded_for" not in nginx
    assert "--no-proxy-headers" in _ENTRYPOINT.read_text(encoding="utf-8")


def test_auth_rate_limit_configuration_matches_all_templates() -> None:
    compose = yaml.safe_load(_COMPOSE_FILE.read_text(encoding="utf-8"))["x-backend-environment"]
    root = _env_values(_REPO_ROOT / ".env.example")
    backend = _env_values(_BACKEND_DIR / ".env.example")
    defaults = {
        "AUTH_LOGIN_ACCOUNT_LIMIT": "10",
        "AUTH_LOGIN_ACCOUNT_WINDOW_SECONDS": "300",
        "AUTH_LOGIN_IP_LIMIT": "30",
        "AUTH_LOGIN_IP_WINDOW_SECONDS": "300",
        "AUTH_REGISTER_ACCOUNT_LIMIT": "3",
        "AUTH_REGISTER_ACCOUNT_WINDOW_SECONDS": "3600",
        "AUTH_REGISTER_IP_LIMIT": "20",
        "AUTH_REGISTER_IP_WINDOW_SECONDS": "3600",
        "AUTH_RATE_LIMIT_REDIS_TIMEOUT_SECONDS": "0.5",
    }
    for name, expected in defaults.items():
        assert root[name] == backend[name] == expected
        assert compose[name].endswith(f":-{expected}}}")
    assert root["AUTH_TRUSTED_PROXY_CIDRS"] == "172.31.250.10/32"
    assert backend["AUTH_TRUSTED_PROXY_CIDRS"] == ""
