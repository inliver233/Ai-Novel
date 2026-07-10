"""Regression coverage for lazy Gemini streaming transport failures."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from app.core.errors import AppError
from app.llm import client as llm_client
from app.llm.messages import ChatMessage
from app.llm.providers.gemini_generate_content import call_gemini_generate_content_stream


_PARTIAL_SSE_LINE = 'data: {"candidates":[{"content":{"parts":[{"text":"partial"}]}}]}'


def _partial_lines_then_timeout() -> Any:
    yield _PARTIAL_SSE_LINE
    raise httpx.ReadTimeout("stream read timed out")


def _make_stream_error_client(*, escape_at: str) -> Any:
    client = MagicMock()
    response = MagicMock()
    response.status_code = 200

    context_manager = MagicMock()
    context_manager.__enter__ = MagicMock(return_value=response)
    context_manager.__exit__ = MagicMock(return_value=False)
    client.stream = MagicMock(return_value=context_manager)

    if escape_at == "stream":
        client.stream.side_effect = httpx.ReadTimeout("stream construction timed out")
    elif escape_at == "enter":
        context_manager.__enter__.side_effect = httpx.ConnectTimeout("stream connect timed out")
    elif escape_at == "iter_lines":
        response.iter_lines.side_effect = _partial_lines_then_timeout
    elif escape_at == "http_error":
        client.stream.side_effect = httpx.ConnectError("stream connect failed")
    else:  # pragma: no cover - test helper guard
        raise ValueError(f"unsupported escape_at: {escape_at}")

    return client


def _build_provider_call_kwargs(client: Any) -> dict[str, Any]:
    return {
        "client": client,
        "base_url": "https://generativelanguage.googleapis.com",
        "model": "gemini-1.5-pro",
        "api_key": "test-key",
        "messages": [ChatMessage(role="user", content="ping")],
        "filtered_params": {},
        "dropped_params": [],
        "timeout": httpx.Timeout(10.0),
        "start": time.perf_counter(),
        "extra": {},
    }


def _assert_timeout_error(exc: AppError) -> None:
    assert exc.code == "LLM_TIMEOUT"
    assert exc.status_code == 504
    assert exc.message == "连接超时，请检查网络或 base_url 是否正确"
    assert isinstance(exc.__cause__, httpx.TimeoutException)


def test_gemini_stream_readtimeout_mid_stream_is_wrapped() -> None:
    client = _make_stream_error_client(escape_at="iter_lines")
    stream_iter, state = call_gemini_generate_content_stream(**_build_provider_call_kwargs(client))

    assert next(stream_iter) == "partial"
    with pytest.raises(AppError) as exc_info:
        next(stream_iter)

    _assert_timeout_error(exc_info.value)
    assert state.latency_ms is not None


@pytest.mark.parametrize("escape_at", ["stream", "enter"])
def test_gemini_stream_readtimeout_at_connect_is_wrapped(escape_at: str) -> None:
    client = _make_stream_error_client(escape_at=escape_at)
    stream_iter, state = call_gemini_generate_content_stream(**_build_provider_call_kwargs(client))

    with pytest.raises(AppError) as exc_info:
        list(stream_iter)

    _assert_timeout_error(exc_info.value)
    assert state.latency_ms is not None


def test_gemini_stream_timeout_public_boundary_adds_llm_context(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_stream_error_client(escape_at="enter")
    monkeypatch.setattr(llm_client, "get_llm_http_client", lambda: client)

    stream_iter, _state = llm_client.call_llm_stream_messages(
        provider="gemini",
        base_url="https://generativelanguage.googleapis.com",
        model="gemini-1.5-pro",
        api_key="test-key",
        messages=[ChatMessage(role="user", content="ping")],
        params={},
        timeout_seconds=7,
    )

    with pytest.raises(AppError) as exc_info:
        list(stream_iter)

    exc = exc_info.value
    _assert_timeout_error(exc)
    assert exc.details == {
        "provider": "gemini",
        "model": "gemini-1.5-pro",
        "timeout_seconds": 7,
        "base_url_host": "generativelanguage.googleapis.com",
    }


def test_gemini_stream_http_error_public_boundary_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_stream_error_client(escape_at="http_error")
    monkeypatch.setattr(llm_client, "get_llm_http_client", lambda: client)

    stream_iter, _state = llm_client.call_llm_stream_messages(
        provider="gemini",
        base_url="https://generativelanguage.googleapis.com",
        model="gemini-1.5-pro",
        api_key="test-key",
        messages=[ChatMessage(role="user", content="ping")],
        params={},
        timeout_seconds=9,
    )

    with pytest.raises(AppError) as exc_info:
        list(stream_iter)

    exc = exc_info.value
    assert exc.code == "LLM_UPSTREAM_ERROR"
    assert exc.status_code == 502
    assert exc.message == "连接失败，请检查网络或 base_url 是否正确"
    assert isinstance(exc.__cause__, httpx.ConnectError)
    assert exc.details == {
        "provider": "gemini",
        "model": "gemini-1.5-pro",
        "timeout_seconds": 9,
        "base_url_host": "generativelanguage.googleapis.com",
    }
