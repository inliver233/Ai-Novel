"""backend-rag-memory#10 残留：pgvector 探活失败必须留痕且不得被长 TTL 钉死。

要求：① 探活异常时 log_event(warning) 留痕（降级不可无痕）；② 探活异常结果只做
短负缓存（快速重探），探活成功得出的稳定结论（含 extension/table 缺失）维持 30s
正缓存；③ 无 bare except。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import vector_storage


@pytest.fixture(autouse=True)
def _reset_ready_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(vector_storage, "_PGVECTOR_READY_CACHE", None)
    yield
    monkeypatch.setattr(vector_storage, "_PGVECTOR_READY_CACHE", None)


class _BoomEngine:
    class dialect:
        name = "postgresql"

    def connect(self):
        raise ConnectionError("probe connection refused")


def test_probe_failure_logs_warning_and_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(vector_storage, "engine", _BoomEngine())
    monkeypatch.setattr(
        vector_storage,
        "log_event",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert vector_storage._pgvector_ready() is False

    assert len(calls) == 1
    args, fields = calls[0]
    assert args == (vector_storage.logger, "warning")
    assert fields["event"] == "VECTOR_RAG"
    assert fields["action"] == "pgvector_ready_probe"
    assert fields["ready"] is False
    assert fields["exception_type"] == "ConnectionError"


def test_probe_failure_uses_short_negative_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vector_storage, "engine", _BoomEngine())
    monkeypatch.setattr(vector_storage, "log_event", lambda *args, **kwargs: None)

    now = 1_000_000.0
    monkeypatch.setattr(vector_storage.time, "time", lambda: now)
    assert vector_storage._pgvector_ready() is False

    cached = vector_storage._PGVECTOR_READY_CACHE
    assert cached is not None
    ready, probed_at, ttl = cached
    assert ready is False
    assert probed_at == now
    assert ttl == vector_storage._PGVECTOR_READY_PROBE_FAILURE_TTL_SECONDS
    assert ttl < vector_storage._PGVECTOR_READY_CACHE_TTL_SECONDS

    # 负缓存过期后必须重探（瞬时故障不可被钉死为持续降级）。
    probe_calls: list[float] = []

    class _CountingBoomEngine(_BoomEngine):
        def connect(self):
            probe_calls.append(now)
            raise ConnectionError("probe connection refused")

    monkeypatch.setattr(vector_storage, "engine", _CountingBoomEngine())
    monkeypatch.setattr(
        vector_storage.time,
        "time",
        lambda: now + vector_storage._PGVECTOR_READY_PROBE_FAILURE_TTL_SECONDS + 0.1,
    )
    assert vector_storage._pgvector_ready() is False
    assert len(probe_calls) == 1


def test_probe_success_keeps_standard_positive_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    class _ScalarResult:
        def __init__(self, value: object) -> None:
            self._value = value

        def scalar(self) -> object:
            return self._value

    class _Conn:
        def execute(self, statement: object) -> _ScalarResult:
            return _ScalarResult(1)

        def __enter__(self) -> "_Conn":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    class _OkEngine:
        class dialect:
            name = "postgresql"

        def connect(self) -> _Conn:
            return _Conn()

    calls: list[object] = []
    monkeypatch.setattr(vector_storage, "engine", _OkEngine())
    monkeypatch.setattr(vector_storage, "log_event", lambda *args, **kwargs: calls.append(args))

    now = 2_000_000.0
    monkeypatch.setattr(vector_storage.time, "time", lambda: now)
    assert vector_storage._pgvector_ready() is True

    cached = vector_storage._PGVECTOR_READY_CACHE
    assert cached is not None
    assert cached == (True, now, vector_storage._PGVECTOR_READY_CACHE_TTL_SECONDS)
    assert calls == []  # 正常探活成功不留 warning
