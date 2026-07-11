from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from app.core.errors import AppError
from app.services.authentication import oidc_client


_DISCOVERY_URL = "https://issuer.example/.well-known/openid-configuration"


def _discovery_payload(*, issuer: str = "https://issuer.example") -> dict[str, str]:
    return {
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "userinfo_endpoint": f"{issuer}/userinfo",
        "issuer": issuer,
    }


@pytest.fixture(autouse=True)
def _clear_discovery_state() -> None:
    with oidc_client._DISCOVERY_STATE_LOCK:
        oidc_client._DISCOVERY_CACHE.clear()
        assert not oidc_client._DISCOVERY_INFLIGHT
    yield
    with oidc_client._DISCOVERY_STATE_LOCK:
        oidc_client._DISCOVERY_CACHE.clear()
        assert not oidc_client._DISCOVERY_INFLIGHT


def test_discovery_singleflight_shares_one_fetch_across_concurrent_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_count = 8
    ready = threading.Barrier(worker_count)
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    call_count = 0
    count_lock = threading.Lock()

    def fake_fetch(discovery_url: str) -> dict[str, str]:
        nonlocal call_count
        assert discovery_url == _DISCOVERY_URL
        with count_lock:
            call_count += 1
        fetch_started.set()
        assert release_fetch.wait(timeout=5)
        return _discovery_payload()

    def worker() -> dict[str, str]:
        ready.wait(timeout=5)
        return oidc_client.get_linuxdo_discovery(
            discovery_url=_DISCOVERY_URL,
            ttl_seconds=300,
        )

    monkeypatch.setattr(oidc_client, "_fetch_linuxdo_discovery", fake_fetch)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(worker) for _ in range(worker_count)]
        assert fetch_started.wait(timeout=5)
        time.sleep(0.05)
        release_fetch.set()
        results = [future.result(timeout=5) for future in futures]

    assert call_count == 1
    assert results == [_discovery_payload()] * worker_count
    assert len({id(result) for result in results}) == worker_count


def test_discovery_cache_hit_has_no_fetch_and_callers_cannot_mutate_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0

    def fake_fetch(_discovery_url: str) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return _discovery_payload()

    monkeypatch.setattr(oidc_client, "_fetch_linuxdo_discovery", fake_fetch)
    first = oidc_client.get_linuxdo_discovery(discovery_url=_DISCOVERY_URL, ttl_seconds=300)
    first["issuer"] = "https://attacker.invalid"
    second = oidc_client.get_linuxdo_discovery(discovery_url=_DISCOVERY_URL, ttl_seconds=300)

    assert call_count == 1
    assert second == _discovery_payload()
    assert first is not second


def test_discovery_url_change_and_monotonic_ttl_expiry_are_cache_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    fetched_urls: list[str] = []

    def fake_fetch(discovery_url: str) -> dict[str, str]:
        fetched_urls.append(discovery_url)
        return _discovery_payload(issuer=discovery_url.rsplit("/", 1)[0])

    monkeypatch.setattr(oidc_client.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(oidc_client, "_fetch_linuxdo_discovery", fake_fetch)

    oidc_client.get_linuxdo_discovery(discovery_url="https://one.example/config", ttl_seconds=10)
    now[0] = 109.999
    oidc_client.get_linuxdo_discovery(discovery_url="https://one.example/config", ttl_seconds=10)
    oidc_client.get_linuxdo_discovery(discovery_url="https://two.example/config", ttl_seconds=10)
    now[0] = 110.0
    oidc_client.get_linuxdo_discovery(discovery_url="https://one.example/config", ttl_seconds=10)

    assert fetched_urls == [
        "https://one.example/config",
        "https://two.example/config",
        "https://one.example/config",
    ]


def test_concurrent_discovery_failure_is_singleflight_and_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_count = 8
    ready = threading.Barrier(worker_count)
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    followers_waiting = threading.Event()
    wait_count = 0
    wait_count_lock = threading.Lock()
    calls = 0
    fail_fetch = True

    class TrackingCondition(threading.Condition):
        def wait(self, timeout: float | None = None) -> bool:
            nonlocal wait_count
            with wait_count_lock:
                wait_count += 1
                if wait_count == worker_count - 1:
                    followers_waiting.set()
            return super().wait(timeout)

    class TrackingFlight(oidc_client._DiscoveryFlight):
        def __init__(self) -> None:
            super().__init__()
            self.condition = TrackingCondition()

    def fake_fetch(_discovery_url: str) -> dict[str, str]:
        nonlocal calls, fail_fetch
        calls += 1
        if fail_fetch:
            fetch_started.set()
            assert release_fetch.wait(timeout=5)
            raise AppError(
                code="OIDC_DISCOVERY_FAILED",
                message="safe discovery failure",
                status_code=502,
                details={"provider": "linuxdo", "error_type": "RuntimeError"},
            )
        return _discovery_payload()

    def worker() -> dict[str, str]:
        ready.wait(timeout=5)
        return oidc_client.get_linuxdo_discovery(
            discovery_url=_DISCOVERY_URL,
            ttl_seconds=300,
        )

    monkeypatch.setattr(oidc_client, "_DiscoveryFlight", TrackingFlight)
    monkeypatch.setattr(oidc_client, "_fetch_linuxdo_discovery", fake_fetch)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(worker) for _ in range(worker_count)]
        assert fetch_started.wait(timeout=5)
        assert followers_waiting.wait(timeout=5)
        release_fetch.set()
        for future in futures:
            with pytest.raises(AppError) as exc_info:
                future.result(timeout=5)
            assert exc_info.value.code == "OIDC_DISCOVERY_FAILED"
            assert exc_info.value.message == "safe discovery failure"
            assert exc_info.value.details == {"provider": "linuxdo", "error_type": "RuntimeError"}

    assert calls == 1
    with oidc_client._DISCOVERY_STATE_LOCK:
        assert not oidc_client._DISCOVERY_INFLIGHT
        assert _DISCOVERY_URL not in oidc_client._DISCOVERY_CACHE

    fail_fetch = False
    assert oidc_client.get_linuxdo_discovery(discovery_url=_DISCOVERY_URL, ttl_seconds=300) == _discovery_payload()
    assert calls == 2


def test_invalid_discovery_response_is_not_cached_and_http_contract_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[object] = [
        {"authorization_endpoint": "https://issuer.example/authorize"},
        _discovery_payload(),
    ]
    client_timeouts: list[float] = []
    request_headers: list[dict[str, str]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return payloads.pop(0)

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            client_timeouts.append(timeout)

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, url: str, *, headers: dict[str, str]) -> FakeResponse:
            assert url == _DISCOVERY_URL
            request_headers.append(headers)
            return FakeResponse()

    monkeypatch.setattr(oidc_client.httpx, "Client", FakeClient)

    with pytest.raises(AppError) as exc_info:
        oidc_client.get_linuxdo_discovery(discovery_url=_DISCOVERY_URL, ttl_seconds=300)
    assert exc_info.value.details == {"provider": "linuxdo", "missing_key": "token_endpoint"}

    assert oidc_client.get_linuxdo_discovery(discovery_url=_DISCOVERY_URL, ttl_seconds=300) == _discovery_payload()
    assert client_timeouts == [10.0, 10.0]
    assert request_headers == [{"Accept": "application/json"}] * 2


def test_discovery_transport_error_is_redacted_and_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            assert timeout == 10.0

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, _url: str, *, headers: dict[str, str]) -> Any:
            nonlocal calls
            calls += 1
            assert headers == {"Accept": "application/json"}
            raise RuntimeError("provider-secret-marker")

    monkeypatch.setattr(oidc_client.httpx, "Client", FakeClient)
    for _ in range(2):
        with pytest.raises(AppError) as exc_info:
            oidc_client.get_linuxdo_discovery(discovery_url=_DISCOVERY_URL, ttl_seconds=300)
        assert exc_info.value.details == {"provider": "linuxdo", "error_type": "RuntimeError"}
        assert "provider-secret-marker" not in str(exc_info.value.details)
        assert "provider-secret-marker" not in str(exc_info.value)
    assert calls == 2


def test_token_exchange_and_userinfo_http_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, object, dict[str, str]]] = []

    class FakeResponse:
        def __init__(self, payload: dict[str, str]) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return self.payload

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            assert timeout == 10.0

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, *, data: object, headers: dict[str, str]) -> FakeResponse:
            calls.append(("POST", url, data, headers))
            return FakeResponse({"access_token": "token"})

        def get(self, url: str, *, headers: dict[str, str]) -> FakeResponse:
            calls.append(("GET", url, None, headers))
            return FakeResponse({"sub": "subject"})

    monkeypatch.setattr(oidc_client.httpx, "Client", FakeClient)
    token = oidc_client.exchange_linuxdo_code_for_token(
        token_endpoint="https://issuer.example/token",
        code="code",
        redirect_uri="https://app.example/callback",
        code_verifier="verifier",
        client_id="client",
        client_secret="secret",
    )
    userinfo = oidc_client.fetch_linuxdo_userinfo(
        userinfo_endpoint="https://issuer.example/userinfo",
        access_token="token",
    )

    assert token == {"access_token": "token"}
    assert userinfo == {"sub": "subject"}
    assert calls == [
        (
            "POST",
            "https://issuer.example/token",
            {
                "grant_type": "authorization_code",
                "code": "code",
                "redirect_uri": "https://app.example/callback",
                "client_id": "client",
                "client_secret": "secret",
                "code_verifier": "verifier",
            },
            {"Accept": "application/json"},
        ),
        (
            "GET",
            "https://issuer.example/userinfo",
            None,
            {"Accept": "application/json", "Authorization": "Bearer token"},
        ),
    ]
