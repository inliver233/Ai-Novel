"""SQLite→Postgres 迁移脚本表覆盖测试。

dep-P2（项目情况完全分析.md）：``scripts/migrate_sqlite_to_postgres.py`` 的
``TABLE_ORDER`` 硬编码了 18 张表名，但实际活 ORM 模型有 31 张表。迁移脚本只复制
``TABLE_ORDER`` 中列出的表（见 main() 的 ``for name in TABLE_ORDER`` 循环，
:222 与 :261），未列入的表会被静默丢弃——README 还把该脚本当官方迁移工具推荐，
导致大规模静默数据丢失（如 ``project_tasks`` / ``entries`` / ``search_documents``
等 15 张活表整表丢失）。

正确行为：``TABLE_ORDER`` 必须覆盖所有活 ORM 模型的表，否则该表数据在迁移时丢失。
本测试断言该正确行为；当前实现有 bug，标 ``known_issue``，故测试真跑真红。

导入方式：``scripts/`` 非 package（无 ``__init__.py``），故用 ``importlib`` 从文件
路径加载 ``TABLE_ORDER``。脚本有 ``if __name__ == "__main__"`` 守卫，``exec_module``
仅执行模块级定义（``TABLE_ORDER`` 赋值 + 一次无害的 ``sys.path.insert``），不触发
``main()``，安全。活表集合取 ``Base.metadata.tables``——导入 ``app.models`` 注册全
部活模型（lite 裁剪后的死模型未注册故不在内，与生产一致）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import app.models  # noqa: F401  注册全部活 ORM 模型进 Base.metadata
from app.db.base import Base

_MIGRATE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_sqlite_to_postgres.py"


def _load_table_order() -> list[str]:
    """从迁移脚本文件安全加载 ``TABLE_ORDER``（scripts/ 非 package，故用 importlib）。"""
    spec = importlib.util.spec_from_file_location("_migrate_sqlite_to_postgres", _MIGRATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.TABLE_ORDER)


# dep-P2: TABLE_ORDER 未覆盖全部活表 → 迁移时未列入的表静默丢失；应覆盖所有活表
@pytest.mark.known_issue
def test_table_order_covers_all_active_tables() -> None:
    table_order = _load_table_order()
    active_tables = set(Base.metadata.tables.keys())

    # 正确行为：所有活 ORM 模型表都必须在 TABLE_ORDER 中（否则迁移时整表丢失）。
    # 当前 bug：18 张表 < 31 张活表，15 张活表未列入 → 断言失败(red)。
    missing = active_tables - set(table_order)
    assert not missing, f"TABLE_ORDER 缺失活表（迁移时会静默丢数据）: {sorted(missing)}"


# dep-P2: TABLE_ORDER 长度应 >= 活表数量（覆盖性必要条件）
@pytest.mark.known_issue
def test_table_order_length_covers_active_count() -> None:
    table_order = _load_table_order()
    active_count = len(Base.metadata.tables)

    # 正确行为：TABLE_ORDER 至少与活表数等长（覆盖所有活表的必要条件）。
    # 当前 bug：len=18 < 31 → 断言失败(red)。
    assert len(table_order) >= active_count, (
        f"TABLE_ORDER 长度 {len(table_order)} < 活表数 {active_count}，无法覆盖全部活表"
    )
