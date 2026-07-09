"""根 conftest —— pytest 全局配置与共享 fixture。

提供四件事：
1. 测试期静默 loguru 业务日志噪声（旧套件实测时 stderr 被业务日志刷屏，掩盖真实失败）。
2. 注册 ``known_issue`` marker（与 pytest.ini 一致，供代码内 @pytest.mark.known_issue 使用）。
3. 共享 fixture（db_factory / app / client / fake_llm），基于 :mod:`tests.support`。
4. 测试期填充固定 Fernet key（见 ``pytest_configure``），让 secret 加密跨平台一致。

既有 unittest 风格测试不依赖这里的 fixture（它们自带脚手架），但会享受到日志静默。
新测试既可用 fixture，也可直接 import ``tests.support`` 的函数。
"""

from __future__ import annotations

import logging
from typing import Iterator

import pytest
from loguru import logger as loguru_logger

from app.core.config import settings as _settings
from tests.support import (
    make_sqlite_engine,
    make_session_factory,
    make_test_app,
    create_tables as _create_tables,
)


# ──────────────────────────────────────────────────────────────
# 1. 日志静默：把 loguru / stdlib logging 在测试期的输出降到最低。
#    （业务日志对测试断言无价值，且会把真实失败淹没在 GBK 乱码中文里。）
# ──────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _silence_app_logging() -> Iterator[None]:
    """测试期静默 loguru / stdlib 日志输出。

    autouse 意味着每个测试都生效——但部分测试（如 test_logging_setup）会调用
    ``configure_logging()`` 清空 loguru 所有 handler，导致本 fixture 注册的
    handler 在 teardown 时已不存在。故 remove 用 try/except 兜底，避免误伤。
    """
    handler_id = loguru_logger.add(lambda _msg: None, level="CRITICAL")
    level = logging.getLogger().level
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        try:
            loguru_logger.remove(handler_id)
        except ValueError:
            # 被测代码已重置 loguru（如 configure_logging 调用了 loguru.remove()）。
            pass
        logging.disable(logging.NOTSET)
        logging.getLogger().setLevel(level)


# ──────────────────────────────────────────────────────────────
# 2. marker 注册（与 pytest.ini 声明一致，便于编辑器/IDE 识别）
# ──────────────────────────────────────────────────────────────
_TEST_FERNET_KEY = "7fBl3GuKPdb-zsPc0uPXZ8xJnDPbazidy-_BsW8owGM="  # 固定测试用 Fernet key（非生产）


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "known_issue: 断言正确行为但当前实现有 bug（失败=待修 backlog）")
    # 测试期统一加密 key：让 encrypt_secret/decrypt_secret 跨平台一致走 Fernet。secrets.py 对
    # "非 win32 + dev + 无 SECRET_ENCRYPTION_KEY" 是 fail-closed（抛 SecretCryptoError，见
    # secrets.py:92），故测试环境必须提供 key，否则 ubuntu CI 上涉及 api_key 加密的用例会红。
    # 仅当未显式配置时填充（本地/CI 可用真实 SECRET_ENCRYPTION_KEY 覆盖）；生产必须由其提供。
    if not _settings.secret_encryption_key:
        _settings.secret_encryption_key = _TEST_FERNET_KEY


# known_issue marker 现仅用于 ``-m`` 过滤分类（诚实镜像语义），不再自动转 xfail。
# known_issue 测试真跑真红——默认 ``pytest -q`` 即诚实暴露现存 bug 数；
# ``-m "not known_issue"`` = 安全网（CI 门禁，必须绿）；``-m known_issue`` = bug 看板。
# 详见 tests/README.md「known_issue 工作流」。



# ──────────────────────────────────────────────────────────────
# 3. 共享 fixture
# ──────────────────────────────────────────────────────────────
@pytest.fixture()
def db_factory():
    """一个测试专用的内存 SQLite 会话工厂（自建引擎、用完 dispose）。

    需要某些表时，在测试里调用 ``create_tables(engine, [ModelA, ModelB])``；
    或直接用下方的 ``tables`` fixture 传入所需模型列表。
    """
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()
