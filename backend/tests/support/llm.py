"""LLM 调用打桩 helper。

既有测试没有共享的 FakeLlmClient 类——惯例是在调用点用 ``unittest.mock.patch``
打桩 ``app.llm.client.call_llm`` 或 ``app.services.generation_service.call_llm_and_record``
（见 ``tests/test_llm_retry.py``）。本模块把这两种最常见的打桩方式封装成 helper，
并提供构造 ``RecordedLlmResult`` 的快捷工厂。

只覆盖最常用的同步调用入口；流式调用形态多变，按需在具体测试里就近打桩更清晰。
"""

from __future__ import annotations

from typing import Any, Iterable
from unittest.mock import patch

from app.services.generation_service import RecordedLlmResult


def make_recorded_result(
    text: str = "ok",
    *,
    finish_reason: str | None = "stop",
    latency_ms: int = 1,
    dropped_params: list[str] | None = None,
    run_id: str = "run-test",
) -> RecordedLlmResult:
    """构造一个成功的 ``RecordedLlmResult``。"""
    return RecordedLlmResult(
        text=text,
        finish_reason=finish_reason,
        latency_ms=latency_ms,
        dropped_params=dropped_params or [],
        run_id=run_id,
    )


def patch_call_llm(returns: Any, *, path: str = "app.llm.client.call_llm"):
    """打桩同步 LLM 调用入口。

    ``returns`` 可以是单个返回值、返回值列表（每次调用返回下一个）或异常实例/异常类
    （交给 ``side_effect``）。返回未启动的 ``patch`` 对象，调用方用 ``with`` 或
    ``start()/stop()`` 控制生命周期，与既有 ``unittest.mock.patch`` 用法一致。

    默认打桩 ``app.llm.client.call_llm``（底层入口）。如果被测代码走
    ``call_llm_and_record``，传 ``path="app.services.generation_service.call_llm_and_record"``。
    """
    if isinstance(returns, list | tuple):
        return patch(path, side_effect=list(returns))
    return patch(path, side_effect=returns) if _is_side_effect(returns) else patch(path, return_value=returns)


def _is_side_effect(value: Any) -> bool:
    """异常类/异常实例/可调用对象视作 side_effect；普通数据视作 return_value。"""
    if isinstance(value, BaseException):
        return True
    if isinstance(value, type) and issubclass(value, BaseException):
        return True
    return callable(value)
