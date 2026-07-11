from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from fastapi import Request
from starlette.testclient import TestClient

from app.core.client_ip import client_ip
from app.core.config import settings
from app.core.errors import AppError
from app.schemas.auth import LocalLoginRequest, LocalRegisterRequest
from app.services.authentication import local, rate_limit
from app.main import app


def _request(*, peer: str = "127.0.0.1", xff: str | None = None) -> Request:
    headers = [] if xff is None else [(b"x-forwarded-for", xff.encode("ascii"))]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers, "client": (peer, 1234)})


def test_untrusted_peer_cannot_spoof_xff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_trusted_proxy_cidrs", "10.0.0.0/8")
    assert client_ip(_request(peer="203.0.113.4", xff="198.51.100.9")) == "203.0.113.4"


def test_trusted_proxy_chain_is_parsed_right_to_left(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_trusted_proxy_cidrs", "10.0.0.0/8,192.0.2.0/24")
    request = _request(peer="10.0.0.2", xff="198.51.100.9, 192.0.2.8")
    assert client_ip(request) == "198.51.100.9"


def test_malformed_xff_falls_back_to_direct_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_trusted_proxy_cidrs", "10.0.0.0/8")
    assert client_ip(_request(peer="10.0.0.2", xff="198.51.100.9, nope")) == "10.0.0.2"


def test_ipv4_mapped_ipv6_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_trusted_proxy_cidrs", "")
    assert client_ip(_request(peer="::ffff:192.0.2.44")) == "192.0.2.44"


def test_empty_xff_and_all_trusted_chain_fall_back_to_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_trusted_proxy_cidrs", "10.0.0.0/8,2001:db8::/32")
    assert client_ip(_request(peer="10.0.0.2", xff="")) == "10.0.0.2"
    assert client_ip(_request(peer="10.0.0.2", xff="2001:db8::1, 10.0.0.3")) == "10.0.0.2"


def test_mixed_ipv4_ipv6_chain_and_malicious_prepend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_trusted_proxy_cidrs", "10.0.0.0/8,2001:db8::/32")
    request = _request(peer="10.0.0.2", xff="203.0.113.66, 198.51.100.7, 2001:db8::4")
    assert client_ip(request) == "198.51.100.7"


def test_local_login_limits_before_any_database_or_bcrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    denied = AppError(code="AUTH_RATE_LIMITED", message="limited", status_code=429)
    monkeypatch.setattr(local, "enforce_auth_rate_limit", Mock(side_effect=denied))
    db = Mock()
    with pytest.raises(AppError) as caught:
        local.local_login(_request(), db, LocalLoginRequest(user_id="alice", password="correct-123"))
    assert caught.value is denied
    db.get.assert_not_called()


def test_local_register_limits_before_database_or_password_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    denied = AppError(code="AUTH_RATE_LIMITED", message="limited", status_code=429)
    monkeypatch.setattr(local, "enforce_auth_rate_limit", Mock(side_effect=denied))
    db = Mock()
    with pytest.raises(AppError) as caught:
        local.local_register(_request(), db, LocalRegisterRequest(user_id=" alice ", password="correct-123"))
    assert caught.value is denied
    db.get.assert_not_called()


def test_production_redis_failure_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_env", "prod")
    monkeypatch.setattr(rate_limit, "_redis_check", Mock(side_effect=TimeoutError))
    with pytest.raises(AppError) as caught:
        rate_limit.enforce_auth_rate_limit(_request(), action="login", account_id="alice")
    assert caught.value.status_code == 503
    assert caught.value.code == "AUTH_RATE_LIMIT_UNAVAILABLE"
    assert caught.value.headers == {"Retry-After": "1"}


def test_development_fallback_enforces_both_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_env", "test")
    monkeypatch.setattr(settings, "auth_login_account_limit", 1)
    monkeypatch.setattr(settings, "auth_login_ip_limit", 10)
    monkeypatch.setattr(rate_limit, "_redis_check", Mock(side_effect=TimeoutError))
    rate_limit.enforce_auth_rate_limit(_request(), action="login", account_id="alice")
    with pytest.raises(AppError) as caught:
        rate_limit.enforce_auth_rate_limit(_request(), action="login", account_id="alice")
    assert caught.value.code == "AUTH_RATE_LIMITED"
    assert int(caught.value.headers["Retry-After"]) >= 1


def test_memory_fallback_ip_bucket_is_shared_across_accounts() -> None:
    policy = rate_limit._Policy(100, 1000, 2, 1000)
    assert rate_limit._memory_check(("account-1", "shared-ip"), policy)[0]
    assert rate_limit._memory_check(("account-2", "shared-ip"), policy)[0]
    assert not rate_limit._memory_check(("account-3", "shared-ip"), policy)[0]


def test_memory_fallback_is_atomic_under_concurrency() -> None:
    policy = rate_limit._Policy(7, 1000, 100, 1000)
    with ThreadPoolExecutor(max_workers=16) as executor:
        outcomes = list(executor.map(lambda _: rate_limit._memory_check(("same-account", "same-ip"), policy)[0], range(32)))
    assert sum(outcomes) == 7


def test_memory_fallback_ttl_does_not_renew_and_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: now[0])
    policy = rate_limit._Policy(1, 1000, 10, 1000)
    assert rate_limit._memory_check(("account", "ip"), policy)[0]
    now[0] = 100.6
    allowed, retry = rate_limit._memory_check(("account", "ip"), policy)
    assert not allowed and retry == 1
    assert rate_limit._MEMORY["account"][1] == 101.0
    now[0] = 101.01
    assert rate_limit._memory_check(("account", "ip"), policy)[0]


def test_memory_fallback_evicts_oldest_entries_over_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rate_limit, "_MEMORY_MAX_ENTRIES", 3)
    now = [10.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: now[0])
    policy = rate_limit._Policy(100, 100_000, 100, 100_000)
    for index in range(3):
        now[0] += 1
        rate_limit._memory_check((f"account-{index}", f"ip-{index}"), policy)
    assert len(rate_limit._MEMORY) <= 3
    assert "account-0" not in rate_limit._MEMORY


def test_http_rate_limit_uses_error_envelope_and_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rate_limit, "_redis_check", Mock(return_value=(False, 9)))
    response = TestClient(app).post(
        "/api/auth/local/login",
        json={"user_id": "rate-limited", "password": "correct-123"},
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "9"
    assert response.json()["error"] == {
        "code": "AUTH_RATE_LIMITED",
        "message": "请求过于频繁，请稍后重试",
        "details": {"action": "login"},
    }
