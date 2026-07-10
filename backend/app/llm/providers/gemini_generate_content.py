from __future__ import annotations

import json
import time
from typing import Any, Callable, Iterator

import httpx

from app.core.errors import AppError
from app.llm.max_tokens import extract_max_tokens_upper_bound

from app.llm.redaction import redact_text
from app.llm.messages import ChatMessage, coalesce_system, merge_consecutive, normalize_role
from app.llm.types import LLMCallResult, LLMStreamState
from app.llm.upstream_errors import map_upstream_error


def _clamp_generation_config_max_output_tokens(
    generation_config: dict[str, Any], *, limit: int, compat_adjustments: list[str]
) -> bool:
    current = generation_config.get("maxOutputTokens")
    if not isinstance(current, int):
        return False
    if current <= limit:
        return False
    generation_config["maxOutputTokens"] = limit
    compat_adjustments.append(f"clamp_maxOutputTokens_{limit}")
    return True


def call_gemini_generate_content(
    *,
    client: httpx.Client,
    base_url: str,
    model: str,
    api_key: str,
    messages: list[ChatMessage],
    filtered_params: dict[str, Any],
    dropped_params: list[str],
    timeout: httpx.Timeout,
    start: float,
    extra: dict[str, Any],
) -> LLMCallResult:
    endpoint = f"{base_url}/v1beta/models/{model}:generateContent"
    url = endpoint

    compat_adjustments: list[str] = []
    compat_dropped_params: list[str] = []

    generation_config: dict[str, Any] = {}
    if "temperature" in filtered_params:
        generation_config["temperature"] = filtered_params["temperature"]
    if "top_p" in filtered_params:
        generation_config["topP"] = filtered_params["top_p"]
    if "max_tokens" in filtered_params:
        generation_config["maxOutputTokens"] = filtered_params["max_tokens"]
    if "top_k" in filtered_params:
        generation_config["topK"] = filtered_params["top_k"]
    if "stop" in filtered_params:
        generation_config["stopSequences"] = filtered_params["stop"]
    thinking_cfg = extra.get("thinkingConfig") or extra.get("thinking_config")
    if isinstance(thinking_cfg, dict):
        generation_config["thinkingConfig"] = thinking_cfg

    normalized = merge_consecutive(messages)
    system, non_system = coalesce_system(normalized)

    contents: list[dict[str, Any]] = []
    for msg in non_system:
        role = normalize_role(msg.role)
        content = msg.content
        if role == "assistant":
            gemini_role = "model"
        elif role == "user":
            gemini_role = "user"
        else:
            gemini_role = "user"
            content = f"[{role.upper()}]\n{content}"
        contents.append({"role": gemini_role, "parts": [{"text": content}]})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": ""}]}]

    payload: dict[str, Any] = {"contents": contents, "generationConfig": generation_config}
    if system.strip():
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    safety = extra.get("safety_settings") or extra.get("safetySettings")
    if safety is not None:
        payload["safetySettings"] = safety

    def post_gemini(payload_obj: dict[str, Any]) -> httpx.Response:
        return client.post(
            url,
            headers={"Content-Type": "application/json", "Accept": "application/json", "x-goog-api-key": api_key},
            json=payload_obj,
            timeout=timeout,
        )

    def drop_generation_config_param(name: str) -> bool:
        if name not in generation_config:
            return False
        generation_config.pop(name, None)
        compat_dropped_params.append(name)
        compat_adjustments.append(f"drop_{name}")
        return True

    resp = post_gemini(payload)
    if resp.status_code in (400, 422):
        upper = extract_max_tokens_upper_bound(redact_text(resp.text))
        if upper is not None and _clamp_generation_config_max_output_tokens(
            generation_config, limit=upper, compat_adjustments=compat_adjustments
        ):
            resp = post_gemini(payload)

        downgrade_steps: list[Callable[[], bool]] = [
            lambda: _clamp_generation_config_max_output_tokens(generation_config, limit=8192, compat_adjustments=compat_adjustments),
            lambda: _clamp_generation_config_max_output_tokens(generation_config, limit=4096, compat_adjustments=compat_adjustments),
            lambda: _clamp_generation_config_max_output_tokens(generation_config, limit=1024, compat_adjustments=compat_adjustments),
            lambda: drop_generation_config_param("thinkingConfig"),
            lambda: drop_generation_config_param("stopSequences"),
            lambda: drop_generation_config_param("topK"),
            lambda: drop_generation_config_param("topP"),
            lambda: drop_generation_config_param("temperature"),
        ]
        for apply in downgrade_steps:
            if resp.status_code not in (400, 422):
                break
            changed = apply()
            if not changed:
                continue
            resp = post_gemini(payload)
    latency_ms = int((time.perf_counter() - start) * 1000)
    if resp.status_code // 100 != 2:
        extra_details = None
        if compat_adjustments:
            extra_details = {
                "compat_adjustments": compat_adjustments,
                "compat_dropped_params": compat_dropped_params,
            }
        raise map_upstream_error(resp.status_code, redact_text(resp.text), extra_details=extra_details)
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        merged_dropped = dropped_params + [p for p in compat_dropped_params if p not in dropped_params]
        return LLMCallResult(text="", latency_ms=latency_ms, dropped_params=merged_dropped, finish_reason=None)
    finish_reason = candidates[0].get("finishReason") if isinstance(candidates[0], dict) else None
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    text_parts = [p.get("text", "") for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)]
    text = "".join(text_parts)
    merged_dropped = dropped_params + [p for p in compat_dropped_params if p not in dropped_params]
    return LLMCallResult(
        text=text,
        latency_ms=latency_ms,
        dropped_params=merged_dropped,
        finish_reason=str(finish_reason) if isinstance(finish_reason, str) else None,
    )


def call_gemini_generate_content_stream(
    *,
    client: httpx.Client,
    base_url: str,
    model: str,
    api_key: str,
    messages: list[ChatMessage],
    filtered_params: dict[str, Any],
    dropped_params: list[str],
    timeout: httpx.Timeout,
    start: float,
    extra: dict[str, Any],
) -> tuple[Iterator[str], LLMStreamState]:
    endpoint = f"{base_url}/v1beta/models/{model}:streamGenerateContent?alt=sse"
    url = endpoint

    generation_config: dict[str, Any] = {}
    if "temperature" in filtered_params:
        generation_config["temperature"] = filtered_params["temperature"]
    if "top_p" in filtered_params:
        generation_config["topP"] = filtered_params["top_p"]
    if "max_tokens" in filtered_params:
        generation_config["maxOutputTokens"] = filtered_params["max_tokens"]
    if "top_k" in filtered_params:
        generation_config["topK"] = filtered_params["top_k"]
    if "stop" in filtered_params:
        generation_config["stopSequences"] = filtered_params["stop"]

    thinking_cfg = extra.get("thinkingConfig") or extra.get("thinking_config")
    if isinstance(thinking_cfg, dict):
        generation_config["thinkingConfig"] = thinking_cfg

    normalized = merge_consecutive(messages)
    system, non_system = coalesce_system(normalized)

    contents: list[dict[str, Any]] = []
    for msg in non_system:
        role = normalize_role(msg.role)
        content = msg.content
        if role == "assistant":
            gemini_role = "model"
        elif role == "user":
            gemini_role = "user"
        else:
            gemini_role = "user"
            content = f"[{role.upper()}]\n{content}"
        contents.append({"role": gemini_role, "parts": [{"text": content}]})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": ""}]}]

    payload: dict[str, Any] = {"contents": contents, "generationConfig": generation_config}
    if system.strip():
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    safety = extra.get("safety_settings") or extra.get("safetySettings")
    if safety is not None:
        payload["safetySettings"] = safety

    state = LLMStreamState(dropped_params=dropped_params)
    compat_adjustments: list[str] = []
    compat_dropped_params: list[str] = []

    def drop_generation_config_param(name: str) -> bool:
        if name not in generation_config:
            return False
        generation_config.pop(name, None)
        compat_dropped_params.append(name)
        compat_adjustments.append(f"drop_{name}")
        return True

    def clamp_max_output_tokens(limit: int) -> bool:
        return _clamp_generation_config_max_output_tokens(generation_config, limit=limit, compat_adjustments=compat_adjustments)

    downgrade_steps: list[Callable[[], bool]] = [
        lambda: clamp_max_output_tokens(8192),
        lambda: clamp_max_output_tokens(4096),
        lambda: clamp_max_output_tokens(1024),
        lambda: drop_generation_config_param("thinkingConfig"),
        lambda: drop_generation_config_param("stopSequences"),
        lambda: drop_generation_config_param("topK"),
        lambda: drop_generation_config_param("topP"),
        lambda: drop_generation_config_param("temperature"),
    ]

    def generator() -> Iterator[str]:
        cm: httpx._client.StreamContextManager[httpx.Response] | None = None
        resp: httpx.Response | None = None
        upstream_text: str | None = None
        try:
            attempts = 0
            while True:
                attempts += 1
                if cm is not None:
                    cm.__exit__(None, None, None)
                    cm = None
                    resp = None

                cm = client.stream(
                    "POST",
                    url,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                        "x-goog-api-key": api_key,
                    },
                    json=payload,
                    timeout=timeout,
                )
                resp = cm.__enter__()
                if resp.status_code // 100 == 2:
                    break

                try:
                    upstream_text = resp.read().decode("utf-8", errors="ignore")
                except Exception:
                    upstream_text = None
                status_code = resp.status_code
                cm.__exit__(None, None, None)
                cm = None
                resp = None

                if status_code in (400, 422) and attempts <= (len(downgrade_steps) + 1):
                    upper = extract_max_tokens_upper_bound(redact_text(upstream_text or ""))
                    if upper is not None and clamp_max_output_tokens(upper):
                        continue
                    changed = False
                    for step in downgrade_steps:
                        if step():
                            changed = True
                            break
                    if changed:
                        continue

                extra_details = None
                if compat_adjustments:
                    extra_details = {
                        "compat_adjustments": compat_adjustments,
                        "compat_dropped_params": compat_dropped_params,
                    }
                raise map_upstream_error(status_code, redact_text(upstream_text or ""), extra_details=extra_details)

            full_text = ""
            for line in resp.iter_lines():
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="ignore")
                line = str(line).strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                if not isinstance(data, dict):
                    continue
                candidates = data.get("candidates") or []
                if not candidates or not isinstance(candidates, list):
                    continue
                first = candidates[0] if candidates else None
                if not isinstance(first, dict):
                    continue

                finish_reason = first.get("finishReason")
                if isinstance(finish_reason, str) and finish_reason:
                    state.finish_reason = finish_reason

                content_obj = first.get("content") or {}
                if not isinstance(content_obj, dict):
                    continue
                parts = content_obj.get("parts") or []
                if not isinstance(parts, list) or not parts:
                    continue
                chunk_text_parts = [p.get("text", "") for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)]
                if not chunk_text_parts:
                    continue
                chunk_text = "".join(chunk_text_parts)
                if not chunk_text:
                    continue

                # Gemini streaming may emit cumulative partial text in each SSE message; only yield the delta.
                if chunk_text.startswith(full_text):
                    delta = chunk_text[len(full_text) :]
                    full_text = chunk_text
                else:
                    delta = chunk_text
                    full_text += chunk_text

                if delta:
                    yield delta
        except httpx.TimeoutException as exc:
            raise AppError(code="LLM_TIMEOUT", message="连接超时，请检查网络或 base_url 是否正确", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise AppError(code="LLM_UPSTREAM_ERROR", message="连接失败，请检查网络或 base_url 是否正确", status_code=502) from exc
        finally:
            state.latency_ms = int((time.perf_counter() - start) * 1000)
            merged_dropped = dropped_params + [p for p in compat_dropped_params if p not in dropped_params]
            state.dropped_params = merged_dropped
            if cm is not None:
                cm.__exit__(None, None, None)

    return generator(), state
