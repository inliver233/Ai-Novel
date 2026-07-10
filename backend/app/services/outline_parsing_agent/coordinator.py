"""Outline parsing orchestrator — dynamic multi-agent pipeline.

Architecture:
  1. Planner agent analyzes content and produces a task decomposition plan
  2. Dynamic sub-agents run in parallel, each with a focused scope
  3. Repair agent recovers broken JSON when extraction agents fail
  4. Validation agent merges and validates all results

The number of agents is not fixed — the planner decides based on content
complexity. Simple outlines get 3 agents, complex ones get 5-10+.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import fields
from typing import Any

from app.api.deps import require_project_editor
from app.core.logging import redact_secrets_text
from app.core.errors import AppError
from app.db.session import SessionLocal
from app.llm.client import call_llm_messages, call_llm_stream_messages
from app.llm.messages import ChatMessage
from app.llm.strategy import DEFAULT_STRATEGY_REGISTRY, LLMStrategy
from app.services.llm_task_catalog import is_supported_llm_task
from app.services.llm_task_preset_resolver import resolve_task_llm_config
from app.services.outline_parsing_agent.agents.dynamic_agent import (
    DynamicExtractionAgent,
    _merge_characters,
    _merge_detailed_outlines,
    _merge_entries,
    _merge_structure,
)
from app.services.outline_parsing_agent.agents.planner_agent import PlannerAgent
from app.services.outline_parsing_agent.agents.repair_agent import RepairAgent
from app.services.outline_parsing_agent.agents.validation_agent import ValidationAgent
from app.services.outline_parsing_agent.chunker import TextChunker
from app.services.outline_parsing_agent.config import AgentPipelineConfig
from app.services.outline_parsing_agent.models import (
    AgentStepResult,
    ParsedDetailedOutline,
    ParseResult,
    SubTask,
    get_agent_display_name,
)

logger = logging.getLogger("ainovel.parsing_agent")


# ---------------------------------------------------------------------------
# LLM strategy adapter
# ---------------------------------------------------------------------------

class _ClientLLMStrategy:
    """LLMStrategy adapter backed by app.llm.client.*"""

    def __init__(self, provider: str) -> None:
        self._provider = str(provider or "").strip()

    def chat_completion(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[ChatMessage],
        params: dict[str, Any],
        timeout_seconds: int,
        extra: dict[str, Any] | None = None,
    ):
        return call_llm_messages(
            provider=self._provider,
            base_url=base_url,
            model=model,
            api_key=api_key,
            messages=messages,
            params=params,
            timeout_seconds=timeout_seconds,
            extra=extra,
        )

    async def stream_completion(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[ChatMessage],
        params: dict[str, Any],
        timeout_seconds: int,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        iterator, _state = call_llm_stream_messages(
            provider=self._provider,
            base_url=base_url,
            model=model,
            api_key=api_key,
            messages=messages,
            params=params,
            timeout_seconds=timeout_seconds,
            extra=extra,
        )
        for delta in iterator:
            yield delta


def _get_llm_strategy(provider: str) -> LLMStrategy:
    try:
        return DEFAULT_STRATEGY_REGISTRY.get_llm(provider)
    except KeyError:
        strategy = _ClientLLMStrategy(provider)
        DEFAULT_STRATEGY_REGISTRY.register_llm(provider, strategy)
        return strategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_pipeline_config(
    agent_config: dict[str, Any] | None,
) -> AgentPipelineConfig:
    allowed = {f.name for f in fields(AgentPipelineConfig)}
    overrides: dict[str, Any] = {}
    if agent_config:
        for key, value in agent_config.items():
            if key in allowed and value is not None:
                overrides[key] = value
    if not overrides:
        return AgentPipelineConfig()
    try:
        return AgentPipelineConfig(**overrides)
    except TypeError:
        return AgentPipelineConfig()


def _build_analysis_context(planner_step: AgentStepResult) -> str:
    """Build analysis context string from planner output for downstream agents."""
    if planner_step.status == "error" or not planner_step.data:
        return ""
    data = planner_step.data
    # Strip task_plan from context (agents don't need it)
    context_data = {k: v for k, v in data.items() if k != "task_plan"}
    try:
        payload = json.dumps(context_data, ensure_ascii=False, indent=2)
    except Exception:
        payload = str(context_data)

    context = f"Analysis context (JSON):\n{payload}"

    complexity = str(data.get("complexity") or "").lower()
    char_count = int(data.get("estimated_character_count") or 0)
    entry_count = int(data.get("estimated_entry_count") or 0)

    hints: list[str] = []
    if complexity == "high" or char_count > 15:
        hints.append(
            f"⚠️ 预计角色约 {char_count} 个，内容复杂度高。"
            "每个角色 profile 控制在 100 字以内，确保 JSON 完整可解析。"
        )
    if complexity == "high" or entry_count > 15:
        hints.append(
            f"⚠️ 预计条目约 {entry_count} 个，内容复杂度高。"
            "每个 content 控制在 150 字以内，确保 JSON 完整可解析。"
        )
    if hints:
        context += "\n\n" + "\n".join(hints)
    return context


def _build_detailed_outlines(data: dict[str, Any]) -> list[ParsedDetailedOutline]:
    """Convert raw merged detailed_outline data into ParsedDetailedOutline objects."""
    outlines_raw = data.get("detailed_outlines")
    if not isinstance(outlines_raw, list):
        return []
    result: list[ParsedDetailedOutline] = []
    for item in outlines_raw:
        if not isinstance(item, dict):
            continue
        vol_num = item.get("volume_number")
        if not isinstance(vol_num, int):
            continue
        result.append(ParsedDetailedOutline(
            volume_number=vol_num,
            volume_title=str(item.get("volume_title") or "").strip(),
            volume_summary=str(item.get("volume_summary") or "").strip(),
            chapters=item.get("chapters") or [],
        ))
    return result


def _error_step(agent_name: str, message: str) -> AgentStepResult:
    safe_message = redact_secrets_text(str(message or "")).replace("\n", " ").strip()[:500] or "unknown error"
    return AgentStepResult(
        agent_name=agent_name,
        status="error",
        duration_ms=0,
        tokens_used=0,
        error_message=safe_message,
    )


def _resolve_outline_llm_preset(
    *,
    project_id: str,
    user_id: str,
    x_llm_provider: str | None,
    x_llm_api_key: str | None,
) -> Any:
    with SessionLocal() as db:
        project = require_project_editor(db, project_id=project_id, user_id=user_id)
        task_key = "outline_parse" if is_supported_llm_task("outline_parse") else "outline_generate"
        resolved = resolve_task_llm_config(
            db, project=project, user_id=user_id, task_key=task_key,
            header_api_key=x_llm_api_key,
        )
        if resolved is None and task_key != "outline_generate":
            resolved = resolve_task_llm_config(
                db, project=project, user_id=user_id, task_key="outline_generate",
                header_api_key=x_llm_api_key,
            )
        if resolved is None:
            raise AppError(code="LLM_CONFIG_ERROR", message="请先在 Prompts 页保存 LLM 配置", status_code=400)
        if x_llm_api_key and x_llm_provider and resolved.llm_call.provider != x_llm_provider:
            raise AppError(code="LLM_CONFIG_ERROR", message="当前任务 provider 与请求头不一致", status_code=400)
        return resolved


def _extract_task_plan(planner_step: AgentStepResult) -> list[SubTask]:
    """Extract validated task plan from planner output."""
    if planner_step.status == "error" or not planner_step.data:
        return _default_task_plan()

    raw_plan = planner_step.data.get("task_plan")
    if not isinstance(raw_plan, list) or not raw_plan:
        return _default_task_plan()

    tasks: list[SubTask] = []
    for item in raw_plan:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("id") or "").strip()
        task_type = str(item.get("type") or "").strip()
        display_name = str(item.get("display_name") or "").strip()
        scope = str(item.get("scope") or "").strip()
        if task_id and task_type in ("structure", "character", "entry", "detailed_outline") and scope:
            tasks.append(SubTask(
                id=task_id,
                type=task_type,
                display_name=display_name or task_type,
                scope=scope,
            ))

    return tasks if tasks else _default_task_plan()


def _default_task_plan() -> list[SubTask]:
    return [
        SubTask(id="structure", type="structure", display_name="大纲骨架",
                scope="提取全部章节结构，包括章节编号、标题和情节节拍"),
        SubTask(id="character", type="character", display_name="角色卡",
                scope="提取全部角色信息，包括姓名、角色定位、背景描述和发展方向"),
        SubTask(id="entry", type="entry", display_name="世界条目",
                scope="提取全部世界观设定条目，包括体系、势力、地点、物品等"),
    ]


def _merge_results_by_type(
    all_results: dict[str, AgentStepResult],
    task_plan: list[SubTask],
) -> dict[str, AgentStepResult]:
    """Merge multiple agent results of the same type into canonical results.

    Returns a dict with keys "structure", "character", "entry", and optionally
    "detailed_outline" — each containing the merged data from all agents of that type.
    """
    # Group results by type
    by_type: dict[str, list[AgentStepResult]] = {
        "structure": [], "character": [], "entry": [], "detailed_outline": [],
    }
    for task in task_plan:
        step = all_results.get(task.id)
        if step and step.status != "error" and step.data:
            by_type[task.type].append(step)

    merged: dict[str, AgentStepResult] = {}

    # Merge structure
    structure_data_list = [s.data for s in by_type["structure"]]
    if structure_data_list:
        merged_data = _merge_structure(structure_data_list)
        total_tokens = sum(s.tokens_used for s in by_type["structure"])
        total_duration = sum(s.duration_ms for s in by_type["structure"])
        all_warnings: list[str] = []
        for s in by_type["structure"]:
            all_warnings.extend(s.warnings)
        merged["structure"] = AgentStepResult(
            agent_name="structure",
            status="success",
            data=merged_data,
            duration_ms=total_duration,
            tokens_used=total_tokens,
            warnings=all_warnings,
        )
    else:
        merged["structure"] = _error_step("structure", "无有效的章节结构结果")

    # Merge characters
    char_data_list = [s.data for s in by_type["character"]]
    if char_data_list:
        merged_data = _merge_characters(char_data_list)
        total_tokens = sum(s.tokens_used for s in by_type["character"])
        total_duration = sum(s.duration_ms for s in by_type["character"])
        all_warnings = []
        for s in by_type["character"]:
            all_warnings.extend(s.warnings)
        merged["character"] = AgentStepResult(
            agent_name="character",
            status="success",
            data=merged_data,
            duration_ms=total_duration,
            tokens_used=total_tokens,
            warnings=all_warnings,
        )
    else:
        merged["character"] = _error_step("character", "无有效的角色结果")

    # Merge entries
    entry_data_list = [s.data for s in by_type["entry"]]
    if entry_data_list:
        merged_data = _merge_entries(entry_data_list)
        total_tokens = sum(s.tokens_used for s in by_type["entry"])
        total_duration = sum(s.duration_ms for s in by_type["entry"])
        all_warnings = []
        for s in by_type["entry"]:
            all_warnings.extend(s.warnings)
        merged["entry"] = AgentStepResult(
            agent_name="entry",
            status="success",
            data=merged_data,
            duration_ms=total_duration,
            tokens_used=total_tokens,
            warnings=all_warnings,
        )
    else:
        merged["entry"] = _error_step("entry", "无有效的条目结果")

    # Merge detailed outlines (optional — only present when planner includes it)
    do_data_list = [s.data for s in by_type["detailed_outline"]]
    if do_data_list:
        merged_data = _merge_detailed_outlines(do_data_list)
        total_tokens = sum(s.tokens_used for s in by_type["detailed_outline"])
        total_duration = sum(s.duration_ms for s in by_type["detailed_outline"])
        all_warnings = []
        for s in by_type["detailed_outline"]:
            all_warnings.extend(s.warnings)
        merged["detailed_outline"] = AgentStepResult(
            agent_name="detailed_outline",
            status="success",
            data=merged_data,
            duration_ms=total_duration,
            tokens_used=total_tokens,
            warnings=all_warnings,
        )

    return merged


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class OutlineParsingOrchestrator:

    def parse_outline(
        self,
        *,
        project_id: str,
        user_id: str,
        content: str,
        request_id: str,
        x_llm_provider: str | None = None,
        x_llm_api_key: str | None = None,
        agent_config: dict[str, Any] | None = None,
    ) -> ParseResult:
        """Synchronous parsing — dynamic multi-agent pipeline."""
        start_time = time.time()

        resolved = _resolve_outline_llm_preset(
            project_id=project_id, user_id=user_id,
            x_llm_provider=x_llm_provider, x_llm_api_key=x_llm_api_key,
        )
        llm_call = resolved.llm_call
        strategy = _get_llm_strategy(llm_call.provider)
        pipeline_config = _build_pipeline_config(agent_config)

        chunks = TextChunker(pipeline_config).chunk(content or "")
        if not chunks:
            return ParseResult(
                agent_log=[_error_step("chunker", "No chunks produced")],
                total_duration_ms=int((time.time() - start_time) * 1000),
                warnings=["chunker: failed to chunk content"],
            )

        llm_kwargs = dict(
            strategy=strategy, base_url=llm_call.base_url,
            api_key=resolved.api_key, model=llm_call.model,
            config=pipeline_config, provider=llm_call.provider,
        )

        # Phase 1: Planner
        planner = PlannerAgent(**llm_kwargs)
        planner_step = planner.run_on_chunks([chunks[0]])
        analysis_context = _build_analysis_context(planner_step)
        task_plan = _extract_task_plan(planner_step)

        # Phase 2: Dynamic extraction
        agents: dict[str, DynamicExtractionAgent] = {}
        for task in task_plan:
            agents[task.id] = DynamicExtractionAgent(
                **llm_kwargs,
                task_id=task.id, task_type=task.type,
                scope=task.scope, display_name=task.display_name,
            )

        results: dict[str, AgentStepResult] = {}
        if pipeline_config.parallel_extraction:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=min(len(agents), 6)) as executor:
                futures = {
                    executor.submit(agent.run_on_chunks, chunks, analysis_context): task_id
                    for task_id, agent in agents.items()
                }
                for future in as_completed(futures):
                    task_id = futures[future]
                    try:
                        results[task_id] = future.result()
                    except Exception as exc:
                        results[task_id] = _error_step(task_id, str(exc))
        else:
            for task_id, agent in agents.items():
                try:
                    results[task_id] = agent.run_on_chunks(chunks, analysis_context)
                except Exception as exc:
                    results[task_id] = _error_step(task_id, str(exc))

        # Phase 3: Repair failed agents
        repair = RepairAgent(**llm_kwargs)
        for task in task_plan:
            step = results.get(task.id)
            if step and step.status == "error" and step._raw_output:
                logger.info("repair_agent: attempting repair for %s", task.id)
                repaired = repair.repair(step._raw_output, task.type)
                if repaired is not None:
                    # Re-parse through the agent's parse_response
                    parsed = agents[task.id].parse_response(repaired)
                    results[task.id] = AgentStepResult(
                        agent_name=task.id,
                        status="success",
                        data=parsed,
                        duration_ms=step.duration_ms,
                        tokens_used=step.tokens_used,
                        warnings=[*step.warnings, f"{task.id}: 经 JSON 修复后解析成功"],
                    )

        # Phase 4: Merge by type & validate
        merged = _merge_results_by_type(results, task_plan)
        validator = ValidationAgent()
        parse_result = validator.validate(
            merged.get("structure") or _error_step("structure", "Missing"),
            merged.get("character") or _error_step("character", "Missing"),
            merged.get("entry") or _error_step("entry", "Missing"),
            planner_step,
        )

        # Attach detailed outlines if present
        do_step = merged.get("detailed_outline")
        if do_step and do_step.status != "error" and do_step.data:
            parse_result.detailed_outlines = _build_detailed_outlines(do_step.data)

        # Include all individual agent steps in the log
        all_agent_steps = [planner_step] + [results[t.id] for t in task_plan if t.id in results]
        parse_result.agent_log = [*all_agent_steps, parse_result.agent_log[-1]]  # Keep validation step at end

        parse_result.total_duration_ms = int((time.time() - start_time) * 1000)
        parse_result.total_tokens_used = sum(
            step.tokens_used for step in parse_result.agent_log if isinstance(step.tokens_used, int)
        )

        logger.info(
            "outline_parse_complete",
            extra={
                "request_id": request_id,
                "project_id": project_id,
                "provider": llm_call.provider,
                "model": llm_call.model,
                "chunks": len(chunks),
                "agents": len(task_plan),
                "duration_ms": parse_result.total_duration_ms,
                "tokens_used": parse_result.total_tokens_used,
                "warnings": len(parse_result.warnings),
            },
        )
        return parse_result

    async def parse_outline_stream_events(
        self,
        *,
        project_id: str,
        user_id: str,
        content: str,
        request_id: str,
        x_llm_provider: str | None = None,
        x_llm_api_key: str | None = None,
        agent_config: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream parsing progress events — dynamic multi-agent pipeline."""
        start_time = time.time()
        try:
            yield {"type": "phase_start", "phase": "analysis", "message": "Running planner agent"}

            resolved = await asyncio.to_thread(
                _resolve_outline_llm_preset,
                project_id=project_id, user_id=user_id,
                x_llm_provider=x_llm_provider, x_llm_api_key=x_llm_api_key,
            )
            llm_call = resolved.llm_call
            strategy = _get_llm_strategy(llm_call.provider)
            pipeline_config = _build_pipeline_config(agent_config)

            chunks = TextChunker(pipeline_config).chunk(content or "")
            if not chunks:
                raise AppError.validation(message="内容为空或无法分块")

            llm_kwargs = dict(
                strategy=strategy, base_url=llm_call.base_url,
                api_key=resolved.api_key, model=llm_call.model,
                config=pipeline_config, provider=llm_call.provider,
            )

            # Phase 1: Planner
            planner = PlannerAgent(**llm_kwargs)
            yield {
                "type": "agent_start",
                "agent": "planner",
                "display_name": get_agent_display_name("planner"),
            }
            planner_step = await asyncio.to_thread(planner.run_on_chunks, [chunks[0]])
            yield {
                "type": "agent_complete",
                "agent": "planner",
                "display_name": get_agent_display_name("planner"),
                "data": dict(planner_step.data) if planner_step.data else {},
                "status": planner_step.status,
                "duration_ms": planner_step.duration_ms,
                "tokens_used": planner_step.tokens_used,
                "warnings": planner_step.warnings,
            }

            analysis_context = _build_analysis_context(planner_step)
            task_plan = _extract_task_plan(planner_step)
            display_names_by_id = {task.id: task.display_name for task in task_plan}

            # Send task plan to frontend so it can create dynamic agent cards
            yield {
                "type": "task_plan",
                "tasks": [
                    {"id": t.id, "type": t.type, "display_name": t.display_name, "scope": t.scope}
                    for t in task_plan
                ],
            }

            # Phase 2: Dynamic extraction
            yield {"type": "phase_start", "phase": "extraction", "message": f"Running {len(task_plan)} extraction agents"}

            agents: dict[str, DynamicExtractionAgent] = {}
            for task in task_plan:
                agents[task.id] = DynamicExtractionAgent(
                    **llm_kwargs,
                    task_id=task.id, task_type=task.type,
                    scope=task.scope, display_name=task.display_name,
                )

            results: dict[str, AgentStepResult] = {}

            if pipeline_config.parallel_extraction:
                import queue as _queue
                from concurrent.futures import ThreadPoolExecutor

                # Emit agent_start for all extraction agents
                for task in task_plan:
                    yield {
                        "type": "agent_start",
                        "agent": task.id,
                        "display_name": task.display_name,
                    }

                event_queue: _queue.Queue[dict[str, Any]] = _queue.Queue()

                def _make_streaming_cb(name: str, display_name: str):
                    last_emit = [0.0]
                    buffer = [""]

                    def cb(delta: str) -> None:
                        buffer[0] += delta
                        now = time.time()
                        if now - last_emit[0] >= 0.5:
                            last_emit[0] = now
                            event_queue.put({
                                "type": "agent_streaming",
                                "agent": name,
                                "display_name": display_name,
                                "text": buffer[0][-200:],
                            })
                    return cb

                def _run_agent(agent, task_id, display_name, _chunks, ctx):
                    try:
                        step = agent.run_on_chunks(
                            _chunks, ctx,
                            on_streaming=_make_streaming_cb(task_id, display_name),
                        )
                        event_queue.put({"type": "_done", "agent": task_id, "result": step})
                    except Exception as exc:
                        event_queue.put({"type": "_done", "agent": task_id, "error": exc})

                max_workers = min(len(task_plan), 6)
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    for task in task_plan:
                        executor.submit(
                            _run_agent, agents[task.id], task.id, task.display_name,
                            chunks, analysis_context,
                        )

                    done_count = 0
                    total_agents = len(task_plan)
                    while done_count < total_agents:
                        try:
                            ev = await asyncio.to_thread(event_queue.get, timeout=0.3)
                        except Exception:
                            await asyncio.sleep(0.1)
                            continue

                        if ev.get("type") == "_done":
                            a_id = ev["agent"]
                            done_count += 1
                            if "error" in ev:
                                step = _error_step(a_id, str(ev["error"]))
                            else:
                                step = ev["result"]
                            results[a_id] = step
                            yield {
                                "type": "agent_complete",
                                "agent": a_id,
                                "display_name": display_names_by_id.get(a_id) or get_agent_display_name(a_id),
                                "data": dict(step.data) if step.data else {},
                                "status": step.status,
                                "duration_ms": step.duration_ms,
                                "tokens_used": step.tokens_used,
                                "warnings": step.warnings,
                            }
                        elif ev.get("type") == "agent_streaming":
                            yield ev
            else:
                # Sequential execution
                for task in task_plan:
                    yield {
                        "type": "agent_start",
                        "agent": task.id,
                        "display_name": task.display_name,
                    }
                    step = await asyncio.to_thread(
                        agents[task.id].run_on_chunks, chunks, analysis_context,
                    )
                    results[task.id] = step
                    yield {
                        "type": "agent_complete",
                        "agent": task.id,
                        "display_name": task.display_name,
                        "data": dict(step.data) if step.data else {},
                        "status": step.status,
                        "duration_ms": step.duration_ms,
                        "tokens_used": step.tokens_used,
                        "warnings": step.warnings,
                    }

            # Phase 3: Repair failed agents
            failed_tasks = [
                task for task in task_plan
                if results.get(task.id) and results[task.id].status == "error"
                and results[task.id]._raw_output
            ]
            if failed_tasks:
                yield {"type": "phase_start", "phase": "repair", "message": f"Repairing {len(failed_tasks)} failed agents"}
                repair = RepairAgent(**llm_kwargs)

                for task in failed_tasks:
                    repair_agent_id = f"repair_{task.id}"
                    yield {
                        "type": "agent_start",
                        "agent": repair_agent_id,
                        "display_name": f"修复: {task.display_name}",
                    }

                    repaired = await asyncio.to_thread(
                        repair.repair, results[task.id]._raw_output, task.type,
                    )

                    if repaired is not None:
                        parsed = agents[task.id].parse_response(repaired)
                        original_step = results[task.id]
                        results[task.id] = AgentStepResult(
                            agent_name=task.id,
                            status="success",
                            data=parsed,
                            duration_ms=original_step.duration_ms,
                            tokens_used=original_step.tokens_used,
                            warnings=[*original_step.warnings, f"{task.id}: 经 JSON 修复后解析成功"],
                        )
                        yield {
                            "type": "agent_complete",
                            "agent": repair_agent_id,
                            "display_name": f"修复: {task.display_name}",
                            "status": "success",
                            "duration_ms": 0,
                            "tokens_used": 0,
                            "warnings": ["修复成功"],
                        }
                        # Also update the original agent card to success
                        yield {
                            "type": "agent_complete",
                            "agent": task.id,
                            "display_name": task.display_name,
                            "data": dict(parsed) if parsed else {},
                            "status": "success",
                            "duration_ms": original_step.duration_ms,
                            "tokens_used": original_step.tokens_used,
                            "warnings": results[task.id].warnings,
                        }
                    else:
                        yield {
                            "type": "agent_complete",
                            "agent": repair_agent_id,
                            "display_name": f"修复: {task.display_name}",
                            "status": "error",
                            "duration_ms": 0,
                            "tokens_used": 0,
                            "warnings": ["修复失败，无法恢复"],
                        }

            # Phase 4: Merge & Validate
            merged = _merge_results_by_type(results, task_plan)
            validator = ValidationAgent()
            yield {
                "type": "agent_start",
                "agent": "validation",
                "display_name": get_agent_display_name("validation"),
            }
            parse_result = await asyncio.to_thread(
                validator.validate,
                merged.get("structure") or _error_step("structure", "Missing"),
                merged.get("character") or _error_step("character", "Missing"),
                merged.get("entry") or _error_step("entry", "Missing"),
                planner_step,
            )

            # Attach detailed outlines if present
            do_step = merged.get("detailed_outline")
            if do_step and do_step.status != "error" and do_step.data:
                parse_result.detailed_outlines = _build_detailed_outlines(do_step.data)

            # Build complete agent log
            all_agent_steps = [planner_step] + [results[t.id] for t in task_plan if t.id in results]
            parse_result.agent_log = [*all_agent_steps, parse_result.agent_log[-1]]

            parse_result.total_duration_ms = int((time.time() - start_time) * 1000)
            parse_result.total_tokens_used = sum(
                step.tokens_used for step in parse_result.agent_log if isinstance(step.tokens_used, int)
            )
            yield {
                "type": "agent_complete",
                "agent": "validation",
                "display_name": get_agent_display_name("validation"),
                "status": "success",
                "duration_ms": 0,
                "tokens_used": 0,
                "warnings": [],
            }
            yield {"type": "parse_complete", "data": parse_result.to_dict()}

        except AppError as exc:
            yield {"type": "error", "message": exc.message, "code": exc.status_code}
        except Exception as exc:
            logger.exception(
                "outline_parse_stream_failed",
                extra={
                    "request_id": request_id,
                    "project_id": project_id,
                    "provider": x_llm_provider,
                    "exception": redact_secrets_text(str(exc)).replace("\n", " ").strip()[:500],
                },
            )
            yield {"type": "error", "message": "解析失败，请稍后重试", "code": 500}


# ---------------------------------------------------------------------------
# Public API (backward compatible)
# ---------------------------------------------------------------------------

def parse_outline(
    *,
    project_id: str,
    user_id: str,
    content: str,
    request_id: str,
    x_llm_provider: str | None = None,
    x_llm_api_key: str | None = None,
    agent_config: dict[str, Any] | None = None,
) -> ParseResult:
    """Parse external outline text into project format using dynamic multi-agent pipeline."""
    return OutlineParsingOrchestrator().parse_outline(
        project_id=project_id, user_id=user_id, content=content,
        request_id=request_id, x_llm_provider=x_llm_provider,
        x_llm_api_key=x_llm_api_key, agent_config=agent_config,
    )


async def parse_outline_stream_events(
    *,
    project_id: str,
    user_id: str,
    content: str,
    request_id: str,
    x_llm_provider: str | None = None,
    x_llm_api_key: str | None = None,
    agent_config: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream parsing progress events."""
    async for event in OutlineParsingOrchestrator().parse_outline_stream_events(
        project_id=project_id, user_id=user_id, content=content,
        request_id=request_id, x_llm_provider=x_llm_provider,
        x_llm_api_key=x_llm_api_key, agent_config=agent_config,
    ):
        yield event
