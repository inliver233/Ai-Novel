from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx

from app.core.errors import AppError


_LINUXDO_PROVIDER = "linuxdo"
_HTTP_TIMEOUT_SECONDS = 10.0
_DISCOVERY_REQUIRED_KEYS = (
    "authorization_endpoint",
    "token_endpoint",
    "userinfo_endpoint",
    "issuer",
)


@dataclass(frozen=True)
class _DiscoveryCacheEntry:
    expires_at: float
    payload: tuple[tuple[str, str], ...]


class _DiscoveryFlight:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.done = False
        self.payload: dict[str, str] | None = None
        self.error: BaseException | None = None


_DISCOVERY_STATE_LOCK = threading.Lock()
_DISCOVERY_CACHE: dict[str, _DiscoveryCacheEntry] = {}
_DISCOVERY_INFLIGHT: dict[str, _DiscoveryFlight] = {}


def _discovery_error(message: str, **details: str) -> AppError:
    return AppError(
        code="OIDC_DISCOVERY_FAILED",
        message=message,
        status_code=502,
        details={"provider": _LINUXDO_PROVIDER, **details},
    )


def _validate_discovery_payload(data: object) -> dict[str, str]:
    if not isinstance(data, dict):
        raise _discovery_error("LinuxDo OIDC discovery 响应无效")

    normalized: dict[str, str] = {}
    for key in _DISCOVERY_REQUIRED_KEYS:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise _discovery_error(
                f"LinuxDo OIDC discovery 缺少字段：{key}",
                missing_key=key,
            )
        normalized[key] = value.strip()
    return normalized


def _fetch_linuxdo_discovery(discovery_url: str) -> dict[str, str]:
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = client.get(discovery_url, headers={"Accept": "application/json"})
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        raise _discovery_error(
            "LinuxDo OIDC discovery 获取失败",
            error_type=type(exc).__name__,
        ) from exc
    return _validate_discovery_payload(data)


def get_linuxdo_discovery(*, discovery_url: str, ttl_seconds: int) -> dict[str, str]:
    """Return validated LinuxDo discovery metadata with per-URL singleflight caching."""

    url = str(discovery_url or "").strip()
    if not url:
        raise _discovery_error(
            "LinuxDo OIDC discovery 获取失败",
            error_type="ValueError",
        )

    now = time.monotonic()
    with _DISCOVERY_STATE_LOCK:
        cached = _DISCOVERY_CACHE.get(url)
        if cached is not None:
            if now < cached.expires_at:
                return dict(cached.payload)
            _DISCOVERY_CACHE.pop(url, None)

        flight = _DISCOVERY_INFLIGHT.get(url)
        is_leader = flight is None
        if flight is None:
            flight = _DiscoveryFlight()
            _DISCOVERY_INFLIGHT[url] = flight

    if not is_leader:
        with flight.condition:
            while not flight.done:
                flight.condition.wait()
            if flight.error is not None:
                raise flight.error
            assert flight.payload is not None
            return dict(flight.payload)

    try:
        payload = _fetch_linuxdo_discovery(url)
        cached_payload = tuple(payload.items())
        expires_at = time.monotonic() + float(ttl_seconds)
        with _DISCOVERY_STATE_LOCK:
            _DISCOVERY_CACHE[url] = _DiscoveryCacheEntry(
                expires_at=expires_at,
                payload=cached_payload,
            )
        with flight.condition:
            flight.payload = dict(payload)
            flight.done = True
            flight.condition.notify_all()
        return dict(payload)
    except BaseException as exc:
        with flight.condition:
            flight.error = exc
            flight.done = True
            flight.condition.notify_all()
        raise
    finally:
        with _DISCOVERY_STATE_LOCK:
            if _DISCOVERY_INFLIGHT.get(url) is flight:
                _DISCOVERY_INFLIGHT.pop(url, None)


def exchange_linuxdo_code_for_token(
    *,
    token_endpoint: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    client_id: str,
    client_secret: str,
) -> dict:
    try:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": code_verifier,
        }
        with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = client.post(
                token_endpoint,
                data=payload,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        raise AppError(
            code="OIDC_TOKEN_EXCHANGE_FAILED",
            message="LinuxDo OIDC token 交换失败",
            status_code=502,
            details={"provider": _LINUXDO_PROVIDER, "error_type": type(exc).__name__},
        ) from exc
    return data if isinstance(data, dict) else {}


def fetch_linuxdo_userinfo(*, userinfo_endpoint: str, access_token: str) -> dict:
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = client.get(
                userinfo_endpoint,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        raise AppError(
            code="OIDC_USERINFO_FAILED",
            message="LinuxDo OIDC userinfo 获取失败",
            status_code=502,
            details={"provider": _LINUXDO_PROVIDER, "error_type": type(exc).__name__},
        ) from exc
    return data if isinstance(data, dict) else {}
