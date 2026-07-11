from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ProcessPoolExecutor

import pytest

from app.services.authentication.rate_limit import _Policy, _key, _redis_check

pytestmark = pytest.mark.skipif(
    os.environ.get("AINOVEL_REDIS_INTEGRATION") != "1",
    reason="requires an explicitly enabled disposable real Redis",
)


def _hit(payload: tuple[str, str, int, int, str]) -> bool:
    account_key, ip_key, limit, window_ms, redis_url = payload
    from app.core.config import settings

    settings.redis_url = redis_url
    allowed, _ = _redis_check(
        (account_key, ip_key),
        _Policy(limit, window_ms, 1000, window_ms),
    )
    return allowed


def test_real_redis_lua_is_atomic_across_processes() -> None:
    redis_url = os.environ["TEST_REDIS_URL"]
    from redis import Redis

    redis = Redis.from_url(redis_url)
    redis.flushdb()
    suffix = uuid.uuid4().hex
    account_key = f"ainovel:auth:{{rate}}:login:account:{suffix}"
    ip_key = f"ainovel:auth:{{rate}}:login:ip:{suffix}"
    payload = (account_key, ip_key, 10, 2000, redis_url)
    with ProcessPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(_hit, [payload] * 32))
    assert sum(results) == 10
    assert int(redis.get(account_key)) == 32
    ttl = redis.pttl(account_key)
    assert 0 < ttl <= 2000
    time.sleep(2.1)
    assert _hit(payload) is True


def test_real_redis_ip_bucket_is_shared_across_accounts() -> None:
    redis_url = os.environ["TEST_REDIS_URL"]
    from app.core.config import settings

    settings.redis_url = redis_url
    suffix = uuid.uuid4().hex
    shared_ip_key = f"ainovel:auth:{{rate}}:login:ip:{suffix}"
    policy = _Policy(100, 2000, 3, 2000)
    outcomes = [
        _redis_check((f"ainovel:auth:{{rate}}:login:account:{suffix}-{index}", shared_ip_key), policy)[0]
        for index in range(4)
    ]
    assert outcomes == [True, True, True, False]


def test_real_redis_orphaned_bucket_ttl_is_repaired() -> None:
    redis_url = os.environ["TEST_REDIS_URL"]
    from app.core.config import settings
    from redis import Redis

    settings.redis_url = redis_url
    suffix = uuid.uuid4().hex
    account_key = f"ainovel:auth:{{rate}}:register:account:{suffix}"
    ip_key = f"ainovel:auth:{{rate}}:register:ip:{suffix}"
    redis = Redis.from_url(redis_url)
    redis.set(account_key, 1)
    allowed, _ = _redis_check((account_key, ip_key), _Policy(10, 1000, 10, 1000))
    assert allowed is True
    assert 0 < redis.pttl(account_key) <= 1000


def test_real_redis_fixed_window_ttl_decreases_and_retry_uses_largest_exceeded_bucket() -> None:
    redis_url = os.environ["TEST_REDIS_URL"]
    from app.core.config import settings
    from redis import Redis

    settings.redis_url = redis_url
    suffix = uuid.uuid4().hex
    account_key = f"ainovel:auth:{{rate}}:login:account:{suffix}"
    ip_key = f"ainovel:auth:{{rate}}:login:ip:{suffix}"
    policy = _Policy(1, 1200, 1, 2200)
    assert _redis_check((account_key, ip_key), policy)[0]
    redis = Redis.from_url(redis_url)
    first_account_ttl = redis.pttl(account_key)
    first_ip_ttl = redis.pttl(ip_key)
    time.sleep(0.25)
    allowed, retry = _redis_check((account_key, ip_key), policy)
    assert not allowed
    second_account_ttl = redis.pttl(account_key)
    second_ip_ttl = redis.pttl(ip_key)
    assert second_account_ttl < first_account_ttl - 100
    assert second_ip_ttl < first_ip_ttl - 100
    assert retry == max(1, (max(second_account_ttl, second_ip_ttl) + 999) // 1000)


def test_real_redis_keys_have_cluster_tag_and_do_not_contain_identity() -> None:
    identity = "private-user@example.test"
    generated = _key("login", "account", identity)
    assert "{rate}" in generated
    assert identity not in generated
    assert generated.startswith("ainovel:auth:{rate}:login:account:")
