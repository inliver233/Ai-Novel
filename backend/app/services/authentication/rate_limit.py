from __future__ import annotations

import hashlib
import hmac
import math
import threading
import time
from dataclasses import dataclass
from typing import Literal

from fastapi import Request

from app.core.auth_session import _get_signing_key
from app.core.client_ip import client_ip
from app.core.config import settings
from app.core.errors import AppError

Action = Literal["login", "register"]

_LUA = """
local result = {}
for i = 1, 2 do
  local limit = tonumber(ARGV[(i - 1) * 2 + 1])
  local window = tonumber(ARGV[(i - 1) * 2 + 2])
  local count
  if redis.call('EXISTS', KEYS[i]) == 0 then
    redis.call('SET', KEYS[i], 1, 'PX', window, 'NX')
    count = 1
  else
    count = redis.call('INCR', KEYS[i])
  end
  local ttl = redis.call('PTTL', KEYS[i])
  if ttl < 0 then
    redis.call('PEXPIRE', KEYS[i], window)
    ttl = window
  end
  result[(i - 1) * 2 + 1] = count
  result[(i - 1) * 2 + 2] = ttl
end
return result
"""


@dataclass(frozen=True, slots=True)
class _Policy:
    account_limit: int
    account_window_ms: int
    ip_limit: int
    ip_window_ms: int


_CLIENTS: dict[tuple[str, float], object] = {}
_CLIENTS_LOCK = threading.Lock()
_MEMORY_LOCK = threading.Lock()
_MEMORY: dict[str, tuple[int, float]] = {}
_MEMORY_MAX_ENTRIES = 10_000


def _policy(action: Action) -> _Policy:
    if action == "login":
        return _Policy(
            settings.auth_login_account_limit,
            settings.auth_login_account_window_seconds * 1000,
            settings.auth_login_ip_limit,
            settings.auth_login_ip_window_seconds * 1000,
        )
    return _Policy(
        settings.auth_register_account_limit,
        settings.auth_register_account_window_seconds * 1000,
        settings.auth_register_ip_limit,
        settings.auth_register_ip_window_seconds * 1000,
    )


def _key(action: Action, bucket: str, identity: str) -> str:
    digest = hmac.new(_get_signing_key(), identity.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"ainovel:auth:{{rate}}:{action}:{bucket}:{digest}"


def _redis_client():
    from redis import Redis

    timeout = float(settings.auth_rate_limit_redis_timeout_seconds)
    cache_key = (settings.redis_url, timeout)
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(cache_key)
        if client is None:
            client = Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=timeout,
                socket_timeout=timeout,
                retry_on_timeout=False,
                decode_responses=False,
            )
            _CLIENTS[cache_key] = client
        return client


def _redis_check(keys: tuple[str, str], policy: _Policy) -> tuple[bool, int]:
    raw = _redis_client().eval(
        _LUA,
        2,
        *keys,
        policy.account_limit,
        policy.account_window_ms,
        policy.ip_limit,
        policy.ip_window_ms,
    )
    account_count, account_ttl, ip_count, ip_ttl = (int(value) for value in raw)
    retry_ms = max(
        account_ttl if account_count > policy.account_limit else 0,
        ip_ttl if ip_count > policy.ip_limit else 0,
    )
    return retry_ms <= 0, max(1, math.ceil(retry_ms / 1000))


def _memory_check(keys: tuple[str, str], policy: _Policy) -> tuple[bool, int]:
    now = time.monotonic()
    windows = (policy.account_window_ms / 1000, policy.ip_window_ms / 1000)
    limits = (policy.account_limit, policy.ip_limit)
    counts: list[int] = []
    ttls: list[float] = []
    with _MEMORY_LOCK:
        expired = [key for key, (_, expires) in _MEMORY.items() if expires <= now]
        for key in expired:
            _MEMORY.pop(key, None)
        for key, window in zip(keys, windows, strict=True):
            count, expires = _MEMORY.get(key, (0, now + window))
            count += 1
            _MEMORY[key] = (count, expires)
            counts.append(count)
            ttls.append(max(0.0, expires - now))
        while len(_MEMORY) > _MEMORY_MAX_ENTRIES:
            oldest = min(_MEMORY, key=lambda item: _MEMORY[item][1])
            _MEMORY.pop(oldest, None)
    retry = max((ttl for count, limit, ttl in zip(counts, limits, ttls, strict=True) if count > limit), default=0)
    return retry <= 0, max(1, math.ceil(retry))


def enforce_auth_rate_limit(request: Request, *, action: Action, account_id: str) -> None:
    policy = _policy(action)
    keys = (_key(action, "account", account_id), _key(action, "ip", client_ip(request)))
    try:
        allowed, retry_after = _redis_check(keys, policy)
    except Exception as exc:
        if settings.app_env == "prod":
            raise AppError(
                code="AUTH_RATE_LIMIT_UNAVAILABLE",
                message="认证服务暂时不可用，请稍后重试",
                status_code=503,
                details={"action": action},
                headers={"Retry-After": "1"},
            ) from exc
        allowed, retry_after = _memory_check(keys, policy)
    if not allowed:
        raise AppError(
            code="AUTH_RATE_LIMITED",
            message="请求过于频繁，请稍后重试",
            status_code=429,
            details={"action": action},
            headers={"Retry-After": str(retry_after)},
        )


def _reset_for_tests() -> None:
    with _CLIENTS_LOCK:
        _CLIENTS.clear()
    with _MEMORY_LOCK:
        _MEMORY.clear()
