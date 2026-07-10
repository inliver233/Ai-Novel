"""Public display-name isolation contracts for outline parsing streams.

The planner may assign the same dynamic agent id in multiple requests while
giving that agent a request-specific display name.  Every stream event must
continue to use the display name from its own task plan, even when requests
execute concurrently or the async generator is advanced one event at a time.

These tests deliberately exercise the package's public stream API instead of
the former process-global display-name registry.  Fake LLM-facing agents keep
the tests deterministic while the real coordinator, parallel extraction path,
event queue, merge, and validation stages remain in use.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from threading import Barrier
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from app.services.outline_parsing_agent import parse_outline_stream_events
from app.services.outline_parsing_agent import coordinator as coordinator_module
from app.services.outline_parsing_agent.models import AgentStepResult


def _planner_step(*, task_id: str, display_name: str) -> AgentStepResult:
    return AgentStepResult(
        agent_name="planner",
        status="success",
        data={
            "task_plan": [
                {
                    "id": task_id,
                    "type": "structure",
                    "display_name": display_name,
                    "scope": "提取请求自己的章节结构",
                }
            ]
        },
    )


def _structure_step(*, task_id: str) -> AgentStepResult:
    return AgentStepResult(
        agent_name=task_id,
        status="success",
        data={"outline_md": "", "volumes": [], "chapters": []},
    )


@contextmanager
def _patched_pipeline(*, task_id: str, extraction_barrier: Barrier | None) -> Iterator[None]:
    class FakePlannerAgent:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run_on_chunks(self, chunks: list[Any]) -> AgentStepResult:
            # The request content is a unique display name in this test harness.
            return _planner_step(task_id=task_id, display_name=chunks[0].text)

    class FakeDynamicExtractionAgent:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run_on_chunks(
            self,
            _chunks: list[Any],
            _analysis_context: str,
            *,
            on_streaming: Any | None = None,
        ) -> AgentStepResult:
            # Both requests have completed planning (and any display-name
            # registration) before either parallel completion event is emitted.
            if extraction_barrier is not None:
                extraction_barrier.wait()
            if on_streaming is not None:
                on_streaming("token")
            return _structure_step(task_id=task_id)

        def parse_response(self, value: Any) -> Any:
            return value

    resolved = SimpleNamespace(
        llm_call=SimpleNamespace(
            provider="test-provider",
            base_url="https://llm.invalid/v1",
            model="test-model",
        ),
        api_key="test-key",
    )

    with (
        patch.object(coordinator_module, "_resolve_outline_llm_preset", return_value=resolved),
        patch.object(coordinator_module, "_get_llm_strategy", return_value=object()),
        patch.object(coordinator_module, "PlannerAgent", FakePlannerAgent),
        patch.object(
            coordinator_module,
            "DynamicExtractionAgent",
            FakeDynamicExtractionAgent,
        ),
    ):
        yield


async def _collect_stream(*, request_id: str, display_name: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    async for event in parse_outline_stream_events(
        project_id="project-test",
        user_id="user-test",
        content=display_name,
        request_id=request_id,
        agent_config={"parallel_extraction": True},
    ):
        events.append(event)
    return events


async def _collect_concurrent_streams(
    *,
    task_id: str,
    display_names: tuple[str, str],
) -> list[tuple[str, list[dict[str, Any]]]]:
    with _patched_pipeline(task_id=task_id, extraction_barrier=Barrier(2, timeout=5)):
        streams = await asyncio.gather(
            _collect_stream(request_id="request-a", display_name=display_names[0]),
            _collect_stream(request_id="request-b", display_name=display_names[1]),
        )
    return list(zip(display_names, streams, strict=True))


def _assert_stream_uses_own_display_name(
    *,
    task_id: str,
    expected_display_name: str,
    events: list[dict[str, Any]],
) -> None:
    task_plan = next(event for event in events if event.get("type") == "task_plan")
    planned_task = next(task for task in task_plan["tasks"] if task["id"] == task_id)
    assert planned_task["display_name"] == expected_display_name

    agent_events = [
        event
        for event in events
        if event.get("agent") == task_id and event.get("type") in {"agent_start", "agent_streaming", "agent_complete"}
    ]
    assert agent_events, f"没有收到动态 agent {task_id!r} 的公开事件"
    assert any(event.get("type") == "agent_complete" for event in agent_events)
    assert {event.get("display_name") for event in agent_events} == {expected_display_name}


# 请求 A/B 使用相同动态 id 时，每条公开 stream 只能看到自己的 display_name。
def test_dynamic_display_name_does_not_leak_across_requests() -> None:
    task_id = "shared_structure_agent"
    display_names = ("请求 A：大纲骨架", "请求 B：大纲骨架")

    streams = asyncio.run(_collect_concurrent_streams(task_id=task_id, display_names=display_names))

    for expected_display_name, events in streams:
        _assert_stream_uses_own_display_name(
            task_id=task_id,
            expected_display_name=expected_display_name,
            events=events,
        )


# Planner 合法复用 built-in id 并给出自定义 label 时，并发请求也不能被静态名或彼此覆盖。
def test_concurrent_requests_do_not_clobber_each_others_display_name() -> None:
    task_id = "structure"
    display_names = ("请求 A：自定义结构提取", "请求 B：自定义结构提取")

    streams = asyncio.run(_collect_concurrent_streams(task_id=task_id, display_names=display_names))

    for expected_display_name, events in streams:
        _assert_stream_uses_own_display_name(
            task_id=task_id,
            expected_display_name=expected_display_name,
            events=events,
        )


def test_display_name_survives_incremental_async_generator_advancement() -> None:
    """The sync SSE bridge advances one async-generator event per task.

    Request-local data must live in the parsing operation itself; state stored
    only in a ContextVar set before an earlier yield would disappear here.
    """

    task_id = "incremental_structure_agent"
    display_name = "逐事件推进：大纲骨架"

    with _patched_pipeline(task_id=task_id, extraction_barrier=None):
        stream: AsyncIterator[dict[str, Any]] = parse_outline_stream_events(
            project_id="project-test",
            user_id="user-test",
            content=display_name,
            request_id="request-incremental",
            agent_config={"parallel_extraction": True},
        )
        loop = asyncio.new_event_loop()
        events: list[dict[str, Any]] = []
        try:
            while True:
                try:
                    events.append(loop.run_until_complete(stream.__anext__()))
                except StopAsyncIteration:
                    break
        finally:
            loop.run_until_complete(stream.aclose())
            loop.close()

    _assert_stream_uses_own_display_name(
        task_id=task_id,
        expected_display_name=display_name,
        events=events,
    )
