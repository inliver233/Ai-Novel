"""backend-data#P3: ensure_db_schema 对遗留 SQLite 库的启发式误盖 head 戳。

catalog backend-data#P3: ``app/db/migrations.py`` 的 ``ensure_db_schema`` 对
「无 alembic_version 的遗留 SQLite 库」用一个【启发式】判定是否已是最新
(:146-:168)：只要库里存在 ``outlines`` + ``llm_profiles`` 两张表，且
``projects`` 含 ``active_outline_id`` / ``llm_profile_id`` 两列，就执行
``alembic stamp head``。但 ``outlines``/``llm_profiles`` 早在 ``1c2a0e6b4c2d``
（紧随 INIT_REVISION ``0f24b611cf21``）就已引入，而当前 head 是
``b1c2d3e4f5a6``——两者之间还有数十个建表迁移。一个只有启发式三信号、却缺少
后续 head 表的「中间态」遗留库会被误盖 ``head`` 戳，随后 ``command.upgrade(
cfg, "head")`` 见已处于 head 便空转(no-op)，所有后续缺失表（例如 head 自带
的 ``vector_rag_profiles``）永远不被创建。

正确行为：``ensure_db_schema`` 跑完后，当前 head 要求的每张表都必须真实存在
（真正迁移完成，而非虚假盖戳）。

本测试构造这样的中间态遗留库，调用 ``ensure_db_schema``，断言 head 自带表
``vector_rag_profiles`` 存在。当前实现因误盖 head 戳而空转 → 表仍缺失 →
断言失败(RED)。修复后（真正建出 head 表）即转绿。
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from app.core.config import settings
from app.db import migrations


def _build_legacy_intermediate_db(db_path: Path) -> None:
    """构造一个【触发启发式】却【缺少后续 head 表】的中间态遗留 SQLite 库。

    只建启发式三信号所需的最小结构：
      - projects(id, active_outline_id, llm_profile_id)
      - outlines(id)
      - llm_profiles(id)
    且【不建】alembic_version，也【不建】任何 ``1c2a0e6b4c2d`` 之后的表
    （如 head 自带的 ``vector_rag_profiles``）。这正是旧版本用 create_all 建库、
    代码升级到新 head 后遗留库的真实形态。
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE projects (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                active_outline_id VARCHAR(36),
                llm_profile_id VARCHAR(36)
            )
            """
        )
        conn.execute("CREATE TABLE outlines (id VARCHAR(36) NOT NULL PRIMARY KEY)")
        conn.execute("CREATE TABLE llm_profiles (id VARCHAR(36) NOT NULL PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()


# backend-data#P3: 启发式误盖 head 戳 -> 缺失 head 表永不被创建
@pytest.mark.known_issue
def test_ensure_db_schema_creates_missing_head_tables_for_legacy_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "legacy.db"
        database_url = f"sqlite:///{db_path.as_posix()}"
        _build_legacy_intermediate_db(db_path)

        # ensure_db_schema 内部读 settings.database_url 构建 alembic cfg；而
        # alembic env.py 的 _get_database_url 又优先读 DATABASE_URL 环境变量。
        # 两处都必须指向临时库；且 app_env != prod（否则 prod 下盖戳被跳过）。
        monkeypatch.setenv("DATABASE_URL", database_url)
        monkeypatch.setattr(settings, "database_url", database_url)
        monkeypatch.setattr(settings, "app_env", "dev")

        engine = create_engine(database_url)
        try:
            migrations.ensure_db_schema(engine=engine)
        finally:
            engine.dispose()

        # 正确行为：ensure_db_schema 跑完后，head 自带的 vector_rag_profiles 表
        # 必须真实存在。当前 bug：启发式误盖 head 戳 -> upgrade head 空转 ->
        # 表从未被建 -> 断言失败(RED)。
        engine2 = create_engine(database_url)
        try:
            tables = set(inspect(engine2).get_table_names())
        finally:
            engine2.dispose()

    assert "vector_rag_profiles" in tables, (
        "ensure_db_schema 误将中间态遗留库盖为 head，未真正迁移，"
        "head 自带表 vector_rag_profiles 仍缺失"
    )
