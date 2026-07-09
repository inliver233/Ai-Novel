"""B 类 known_issue 测试 —— Gemini 流式生成 httpx.ReadTimeout 裸逃逸（catalog H25）。

诚实镜像：断言【正确行为】= 流式生成中 httpx.ReadTimeout 应被捕获并转为封装错误
（AppError，如 LLM_TIMEOUT），不应作为裸 httpx 异常逃逸到 StreamingResponse。

当前 ``app/llm/providers/gemini_generate_content.py`` 的流式 generator 只有 finally、
没有 ``except httpx.*`` → ``httpx.ReadTimeout`` 直接逃逸生成器，绕过
``call_llm_stream_messages`` 的 ``try/except AppError``（生成器是惰性的，异常在迭代
而非创建时抛出）与 ``_attach_llm_error_context``。故本测试必须 FAILED（红）。
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from app.core.errors import AppError
from app.llm.messages import ChatMessage
from app.llm.providers.gemini_generate_content import call_gemini_generate_content_stream


def _make_stream_timeout_client(*, escape_at: str) -> Any:
    """构造假的 httpx.Client，其 ``stream`` 在指定环节抛 ``httpx.ReadTimeout``。

    escape_at:
      - "iter_lines": 连接建立成功（200），但读取 SSE 行时超时（流式中途超时，最贴近 H25）。
      - "enter": 建立/进入流连接时即超时。
    """
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 200

    cm = MagicMock()
    if escape_at == "enter":
        cm.__enter__ = MagicMock(side_effect=httpx.ReadTimeout("stream connect timed out"))
    else:
        cm.__enter__ = MagicMock(return_value=resp)
        resp.iter_lines = MagicMock(side_effect=httpx.ReadTimeout("stream read timed out"))
    cm.__exit__ = MagicMock(return_value=False)

    client.stream = MagicMock(return_value=cm)
    return client


def _build_call_kwargs(client: Any) -> dict[str, Any]:
    return dict(
        client=client,
        base_url="https://generativelanguage.googleapis.com",
        model="gemini-1.5-pro",
        api_key="test-key",
        messages=[ChatMessage(role="user", content="ping")],
        filtered_params={},
        dropped_params=[],
        timeout=httpx.Timeout(10.0),
        start=time.perf_counter(),
        extra={},
    )


def _drain_and_inspect(gen: Any) -> Exception | None:
    """耗尽生成器，返回逃逸的异常（若无则 None）。"""
    try:
        list(gen)
    except Exception as exc:
        return exc
    return None


@pytest.mark.known_issue
def test_gemini_stream_readtimeout_mid_stream_is_wrapped() -> None:
    # H25: 流式读取 SSE 行时 httpx.ReadTimeout 应被捕获并转为 AppError，而非裸逃逸。
    client = _make_stream_timeout_client(escape_at="iter_lines")
    gen, _state = call_gemini_generate_content_stream(**_build_call_kwargs(client))

    raised = _drain_and_inspect(gen)
    assert raised is not None, "expected the stream to surface an error on httpx.ReadTimeout"
    # 正确行为：逃逸的应是封装后的 AppError（如 LLM_TIMEOUT），而非裸 httpx.ReadTimeout。
    assert isinstance(raised, AppError), (
        f"httpx.ReadTimeout escaped bare (got {type(raised).__name__}); expected a wrapped AppError"
    )


@pytest.mark.known_issue
def test_gemini_stream_readtimeout_at_connect_is_wrapped() -> None:
    # H25: 建立/进入流连接时 httpx.ReadTimeout 应被捕获并转为 AppError，而非裸逃逸。
    client = _make_stream_timeout_client(escape_at="enter")
    gen, _state = call_gemini_generate_content_stream(**_build_call_kwargs(client))

    raised = _drain_and_inspect(gen)
    assert raised is not None, "expected the stream to surface an error on httpx.ReadTimeout"
    assert isinstance(raised, AppError), (
        f"httpx.ReadTimeout escaped bare (got {type(raised).__name__}); expected a wrapped AppError"
    )
