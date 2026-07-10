from __future__ import annotations

import json
import time
from typing import Any, Iterator
from urllib.parse import urlsplit

import httpx

from app.core.errors import AppError
from app.llm.http_client import get_llm_http_client
from app.llm.messages import ChatMessage
from app.llm.types import LLMCallResult, LLMStreamState
from app.llm.utils import normalize_base_url


def _filter_params(provider: str, params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    supported: set[str]
    if provider in ("openai", "openai_responses"):
        supported = {"temperature", "top_p", "max_tokens", "presence_penalty", "frequency_penalty", "stop"}
    elif provider in ("openai_compatible", "openai_responses_compatible"):
        supported = {"temperature", "top_p", "max_tokens", "stop"}
    elif provider == "anthropic":
        supported = {"temperature", "top_p", "max_tokens", "top_k", "stop"}
    elif provider == "gemini":
        supported = {"temperature", "top_p", "max_tokens", "top_k", "stop"}
    else:
        supported = set()

    filtered: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in params.items():
        if value is None:
            continue
        if key == "stop" and isinstance(value, list) and not value:
            continue
        if key in supported:
            filtered[key] = value
        else:
            dropped.append(key)
    return filtered, dropped


def _llm_base_url_host(base_url: str | None) -> str | None:
    raw = str(base_url or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    host = str(parts.netloc or "").strip()
    return host or None


def _llm_error_context(*, provider: str, base_url: str, model: str, timeout_seconds: int) -> dict[str, Any]:
    context: dict[str, Any] = {
        "provider": str(provider or "").strip(),
        "model": str(model or "").strip(),
        "timeout_seconds": int(timeout_seconds),
    }
    host = _llm_base_url_host(base_url)
    if host:
        context["base_url_host"] = host
    return context


def _attach_llm_error_context(
    exc: AppError,
    *,
    provider: str,
    base_url: str,
    model: str,
    timeout_seconds: int,
) -> None:
    exc.details = {
        **_llm_error_context(
            provider=provider,
            base_url=base_url,
            model=model,
            timeout_seconds=timeout_seconds,
        ),
        **(exc.details or {}),
    }


def _iter_llm_stream_with_error_context(
    stream_iter: Iterator[str],
    *,
    provider: str,
    base_url: str,
    model: str,
    timeout_seconds: int,
) -> Iterator[str]:
    try:
        yield from stream_iter
    except AppError as exc:
        _attach_llm_error_context(
            exc,
            provider=provider,
            base_url=base_url,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        raise
    except httpx.TimeoutException as exc:
        raise AppError(
            code="LLM_TIMEOUT",
            message="连接超时，请检查网络或 base_url 是否正确",
            status_code=504,
            details=_llm_error_context(
                provider=provider,
                base_url=base_url,
                model=model,
                timeout_seconds=timeout_seconds,
            ),
        ) from exc
    except httpx.HTTPError as exc:
        raise AppError(
            code="LLM_UPSTREAM_ERROR",
            message="连接失败，请检查网络或 base_url 是否正确",
            status_code=502,
            details=_llm_error_context(
                provider=provider,
                base_url=base_url,
                model=model,
                timeout_seconds=timeout_seconds,
            ),
        ) from exc


def call_llm_stream(
    *,
    provider: str,
    base_url: str,
    model: str,
    api_key: str,
    system: str,
    user: str,
    params: dict[str, Any],
    timeout_seconds: int,
    extra: dict[str, Any] | None = None,
) -> tuple[Iterator[str], LLMStreamState]:
    messages: list[ChatMessage] = []
    if system.strip():
        messages.append(ChatMessage(role="system", content=system))
    messages.append(ChatMessage(role="user", content=user))
    return call_llm_stream_messages(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key,
        messages=messages,
        params=params,
        timeout_seconds=timeout_seconds,
        extra=extra,
    )


def call_llm_stream_messages(
    *,
    provider: str,
    base_url: str,
    model: str,
    api_key: str,
    messages: list[ChatMessage],
    params: dict[str, Any],
    timeout_seconds: int,
    extra: dict[str, Any] | None = None,
) -> tuple[Iterator[str], LLMStreamState]:
    if not api_key:
        raise AppError(code="LLM_KEY_MISSING", message="缺少 API Key（请在 Prompts 页填写）", status_code=401)

    base_url = normalize_base_url(base_url)
    filtered_params, dropped = _filter_params(provider, params)
    extra = extra or {}

    start = time.perf_counter()
    client = get_llm_http_client()
    read_timeout = max(1.0, float(timeout_seconds))
    connect_timeout = min(10.0, read_timeout)
    write_timeout = min(10.0, read_timeout)
    pool_timeout = min(10.0, read_timeout)
    timeout = httpx.Timeout(connect=connect_timeout, read=read_timeout, write=write_timeout, pool=pool_timeout)

    def _with_error_context(result: tuple[Iterator[str], LLMStreamState]) -> tuple[Iterator[str], LLMStreamState]:
        stream_iter, state = result
        return (
            _iter_llm_stream_with_error_context(
                stream_iter,
                provider=provider,
                base_url=base_url,
                model=model,
                timeout_seconds=timeout_seconds,
            ),
            state,
        )

    try:
        if provider in ("openai", "openai_compatible"):
            from app.llm.providers.openai_chat import call_openai_chat_completions_stream

            return _with_error_context(
                call_openai_chat_completions_stream(
                    client=client,
                    provider=provider,
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                    messages=messages,
                    filtered_params=filtered_params,
                    dropped_params=dropped,
                    timeout=timeout,
                    start=start,
                    extra=extra,
                )
            )

        if provider in ("openai_responses", "openai_responses_compatible"):
            from app.llm.providers.openai_responses import call_openai_responses_stream

            return _with_error_context(
                call_openai_responses_stream(
                    client=client,
                    provider=provider,
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                    messages=messages,
                    filtered_params=filtered_params,
                    dropped_params=dropped,
                    timeout=timeout,
                    start=start,
                    extra=extra,
                )
            )

        if provider == "anthropic":
            from app.llm.providers.anthropic_messages import call_anthropic_messages_stream

            return _with_error_context(
                call_anthropic_messages_stream(
                    client=client,
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                    messages=messages,
                    filtered_params=filtered_params,
                    dropped_params=dropped,
                    timeout=timeout,
                    start=start,
                    extra=extra,
                )
            )

        if provider == "gemini":
            from app.llm.providers.gemini_generate_content import call_gemini_generate_content_stream

            return _with_error_context(
                call_gemini_generate_content_stream(
                    client=client,
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                    messages=messages,
                    filtered_params=filtered_params,
                    dropped_params=dropped,
                    timeout=timeout,
                    start=start,
                    extra=extra,
                )
            )

        raise AppError(code="LLM_CONFIG_ERROR", message="不支持的 provider", status_code=400)
    except AppError as exc:
        _attach_llm_error_context(
            exc,
            provider=provider,
            base_url=base_url,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        raise


def call_llm(
    *,
    provider: str,
    base_url: str,
    model: str,
    api_key: str,
    system: str,
    user: str,
    params: dict[str, Any],
    timeout_seconds: int,
    extra: dict[str, Any] | None = None,
) -> LLMCallResult:
    messages: list[ChatMessage] = []
    if system.strip():
        messages.append(ChatMessage(role="system", content=system))
    messages.append(ChatMessage(role="user", content=user))
    return call_llm_messages(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key,
        messages=messages,
        params=params,
        timeout_seconds=timeout_seconds,
        extra=extra,
    )


def call_llm_messages(
    *,
    provider: str,
    base_url: str,
    model: str,
    api_key: str,
    messages: list[ChatMessage],
    params: dict[str, Any],
    timeout_seconds: int,
    extra: dict[str, Any] | None = None,
) -> LLMCallResult:
    if not api_key:
        raise AppError(code="LLM_KEY_MISSING", message="缺少 API Key（请在 Prompts 页填写）", status_code=401)

    base_url = normalize_base_url(base_url)
    filtered_params, dropped = _filter_params(provider, params)
    extra = extra or {}

    start = time.perf_counter()
    try:
        client = get_llm_http_client()
        read_timeout = max(1.0, float(timeout_seconds))
        connect_timeout = min(10.0, read_timeout)
        write_timeout = min(10.0, read_timeout)
        pool_timeout = min(10.0, read_timeout)
        timeout = httpx.Timeout(connect=connect_timeout, read=read_timeout, write=write_timeout, pool=pool_timeout)

        if provider in ("openai", "openai_compatible"):
            from app.llm.providers.openai_chat import call_openai_chat_completions

            return call_openai_chat_completions(
                client=client,
                provider=provider,
                base_url=base_url,
                model=model,
                api_key=api_key,
                messages=messages,
                filtered_params=filtered_params,
                dropped_params=dropped,
                timeout=timeout,
                start=start,
                extra=extra,
            )

        if provider in ("openai_responses", "openai_responses_compatible"):
            from app.llm.providers.openai_responses import call_openai_responses

            return call_openai_responses(
                client=client,
                provider=provider,
                base_url=base_url,
                model=model,
                api_key=api_key,
                messages=messages,
                filtered_params=filtered_params,
                dropped_params=dropped,
                timeout=timeout,
                start=start,
                extra=extra,
            )

        if provider == "anthropic":
            from app.llm.providers.anthropic_messages import call_anthropic_messages

            return call_anthropic_messages(
                client=client,
                base_url=base_url,
                model=model,
                api_key=api_key,
                messages=messages,
                filtered_params=filtered_params,
                dropped_params=dropped,
                timeout=timeout,
                start=start,
                extra=extra,
            )

        if provider == "gemini":
            from app.llm.providers.gemini_generate_content import call_gemini_generate_content

            return call_gemini_generate_content(
                client=client,
                base_url=base_url,
                model=model,
                api_key=api_key,
                messages=messages,
                filtered_params=filtered_params,
                dropped_params=dropped,
                timeout=timeout,
                start=start,
                extra=extra,
            )

        raise AppError(code="LLM_CONFIG_ERROR", message="不支持的 provider", status_code=400)
    except AppError as exc:
        _attach_llm_error_context(
            exc,
            provider=provider,
            base_url=base_url,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        raise
    except httpx.TimeoutException as exc:
        raise AppError(
            code="LLM_TIMEOUT",
            message="连接超时，请检查网络或 base_url 是否正确",
            status_code=504,
            details=_llm_error_context(
                provider=provider,
                base_url=base_url,
                model=model,
                timeout_seconds=timeout_seconds,
            ),
        ) from exc
    except httpx.HTTPError as exc:
        raise AppError(
            code="LLM_UPSTREAM_ERROR",
            message="连接失败，请检查网络或 base_url 是否正确",
            status_code=502,
            details=_llm_error_context(
                provider=provider,
                base_url=base_url,
                model=model,
                timeout_seconds=timeout_seconds,
            ),
        ) from exc
    except json.JSONDecodeError as exc:
        raise AppError(
            code="LLM_UPSTREAM_ERROR",
            message="上游响应解析失败",
            status_code=502,
            details=_llm_error_context(
                provider=provider,
                base_url=base_url,
                model=model,
                timeout_seconds=timeout_seconds,
            ),
        ) from exc
