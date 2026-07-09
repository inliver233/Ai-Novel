"""大纲解析 agent display_name 注册表的隔离契约测试。  # H20

catalog H20：``app/services/outline_parsing_agent/models.py`` 的
``_dynamic_display_names`` 是**进程全局 dict**，``register_agent_display_name`` /
``get_agent_display_name`` 仅以 ``agent_id`` 为 key，**不带 user_id / request_id /
project_id 任何隔离维度**。多用户并发解析时：

  - 请求 A 注册的 display_name 会被请求 B 的同 agent_id 写入覆盖（last-write-wins）；
  - 请求 B 即使从未注册过某 agent，也会读到请求 A 残留的值（跨请求泄漏）；
  - 字典永不清理 → 内存泄漏 + 显示串话。

本测试断言【正确行为】：不同请求/用户的 display_name 查询应互相隔离——为 userA
设置的 display_name 不应被 userB 的查询读到。当前实现有 bug，故标记
``known_issue``（诚实镜像：真跑真红；修复后自动转绿）。

注：``register_agent_display_name`` / ``get_agent_display_name`` 是模块级公开
函数（非 ``_`` 前缀），直接调用以测其隔离契约正当。``_dynamic_display_names``
本身是 ``_`` 前缀私有 dict，仅在 try/finally 中快照+还原用于测试隔离清理，不
断言其内部结构。``AGENT_DISPLAY_NAMES``（内置常量）代码从不修改，无需清理。

数据串话仅对【非内置】agent_id 可见：``get_agent_display_name`` 用 ``or``
短路——内置 key（如 ``structure``）的真值恒胜，动态注册对内置 key 是死代码。
故用非内置 key（``repair_<taskid>`` 形态，见 coordinator.py:713）方能让
``_dynamic_display_names`` 真正参与解析，暴露隔离缺失。
"""

from __future__ import annotations

import pytest

from app.services.outline_parsing_agent import models as opa_models
from app.services.outline_parsing_agent.models import (
    get_agent_display_name,
    register_agent_display_name,
)


@pytest.fixture()
def isolated_dynamic_display_names():
    """H20: 快照并清空 ``_dynamic_display_names``，测试后还原。

    该 dict 是进程全局可变状态，不清理会污染同进程其他测试（正是 bug 的另一面：
    代码自身从不清理它）。每个测试从空 dict 起步，结束后还原原内容。
    """
    global_dict = opa_models._dynamic_display_names  # noqa: SLF001 — 测试清理用
    snapshot = dict(global_dict)
    global_dict.clear()
    try:
        yield
    finally:
        global_dict.clear()
        global_dict.update(snapshot)


# 请求 A 注册的动态 agent display_name 不应被请求 B（从未注册）读到
@pytest.mark.known_issue  # H20
def test_dynamic_display_name_does_not_leak_across_requests(
    isolated_dynamic_display_names: None,
) -> None:
    request_a_agent = "repair_structure"  # coordinator.py:713 形态的非内置 agent_id
    request_a_label = "修复: 请求A的大纲骨架"

    # 请求 A 的解析管线注册了 repair_structure 的 display_name
    register_agent_display_name(request_a_agent, request_a_label)

    # 请求 B（独立用户/请求，从未注册任何 display_name）查询同一 agent_id
    # 正确行为：请求 B 应拿到回退值（裸 agent_id），而非请求 A 残留的值
    # 当前 bug：全局 dict 无隔离 → 请求 B 读到请求 A 的值 → 断言失败(red)
    leaked = get_agent_display_name(request_a_agent)
    assert leaked == request_a_agent, (
        f"跨请求泄漏：请求 B 读到请求 A 注册的 display_name {leaked!r}，"
        f"期望回退值 {request_a_agent!r}（display_name 注册表应按请求隔离）"
    )


# 并发请求注册同 agent_id 时，各自查对应拿到自己的值，而非 last-write-wins
@pytest.mark.known_issue  # H20
def test_concurrent_requests_do_not_clobber_each_others_display_name(
    isolated_dynamic_display_names: None,
) -> None:
    agent_id = "repair_character"  # 非内置 agent_id，让 _dynamic_display_names 参与解析
    request_a_label = "修复: 请求A的角色卡"
    request_b_label = "修复: 请求B的角色卡"

    # 请求 A 先注册
    register_agent_display_name(agent_id, request_a_label)
    # 请求 B 并发注册同名 agent（合法：不同请求各自有自己的 repair 流程）
    register_agent_display_name(agent_id, request_b_label)

    # 请求 A 重新查询自己注册的 agent 的 display_name
    # 正确行为：请求 A 拿到自己的值 request_a_label
    # 当前 bug：全局 dict 按 agent_id 单维度索引 → 请求 B 覆盖请求 A → 串话
    seen_by_a = get_agent_display_name(agent_id)
    assert seen_by_a == request_a_label, (
        f"并发串话：请求 A 的 display_name 被请求 B 覆盖为 {seen_by_a!r}，"
        f"期望请求 A 自己的值 {request_a_label!r}（注册表应按请求隔离，非 last-write-wins）"
    )
