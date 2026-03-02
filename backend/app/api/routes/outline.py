from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Header, Request
from sqlalchemy import select

from app.api.deps import DbDep, UserIdDep, require_project_editor, require_project_viewer
from app.core.errors import AppError, ok_payload
from app.core.logging import log_event
from app.db.session import SessionLocal
from app.llm.capabilities import max_output_tokens_limit
from app.llm.client import call_llm_stream_messages
from app.models.character import Character
from app.models.llm_preset import LLMPreset
from app.models.project_settings import ProjectSettings
from app.schemas.outline_generate import OutlineGenerateRequest
from app.services.generation_service import build_run_params_json, call_llm_and_record, prepare_llm_call, with_param_overrides
from app.services.llm_key_resolver import resolve_api_key_for_project
from app.services.outline_store import ensure_active_outline
from app.services.output_contracts import build_repair_prompt_for_task, contract_for_task
from app.services.prompt_presets import render_preset_for_task
from app.services.prompt_store import format_characters
from app.services.run_store import write_generation_run
from app.services.search_index_service import schedule_search_rebuild_task
from app.services.style_resolution_service import resolve_style_guide
from app.services.vector_rag_service import schedule_vector_rebuild_task
from app.utils.sse_response import (
    create_sse_response,
    sse_chunk,
    sse_done,
    sse_error,
    sse_heartbeat,
    sse_progress,
    sse_result,
)
from app.models.outline import Outline
from app.schemas.outline import OutlineOut, OutlineUpdate

router = APIRouter()
logger = logging.getLogger("ainovel")


def _mark_vector_index_dirty(db: DbDep, *, project_id: str) -> None:
    row = db.get(ProjectSettings, project_id)
    if row is None:
        row = ProjectSettings(project_id=project_id)
        db.add(row)
        db.flush()
    row.vector_index_dirty = True


def _extract_target_chapter_count(requirements: dict[str, object] | None) -> int | None:
    if not isinstance(requirements, dict):
        return None
    raw = requirements.get("chapter_count")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return None
            value = int(text)
        else:
            value = int(raw)
    except Exception:
        return None
    if value <= 0:
        return None
    # Keep a sanity cap for prompt safety.
    return min(value, 2000)


def _build_outline_generation_guidance(target_chapter_count: int | None) -> dict[str, str]:
    if not target_chapter_count:
        return {
            "chapter_count_rule": "",
            "chapter_detail_rule": "beats 每章 5~9 条，按发生顺序；每条用短句，明确“发生了什么/造成什么后果”。",
        }
    if target_chapter_count <= 20:
        detail = "beats 每章 5~9 条，按发生顺序；每条用短句，明确“发生了什么/造成什么后果”。"
    elif target_chapter_count <= 60:
        detail = "beats 每章 3~5 条，保持因果推进；每条保持短句，避免冗长。"
    elif target_chapter_count <= 120:
        detail = "beats 每章 2~3 条，只保留主冲突与关键转折，保证节奏连续。"
    else:
        detail = "beats 每章 1~2 条，极简表达关键推进；若长度受限，优先保留章节覆盖与编号完整。"
    return {
        "chapter_count_rule": (
            f"chapters 必须输出 {target_chapter_count} 章，number 需完整覆盖 1..{target_chapter_count} 且不缺号。"
        ),
        "chapter_detail_rule": detail,
    }


def _recommend_outline_max_tokens(
    *,
    target_chapter_count: int | None,
    provider: str,
    model: str | None,
    current_max_tokens: int | None,
) -> int | None:
    if not target_chapter_count or target_chapter_count <= 20:
        return None
    if target_chapter_count <= 60:
        wanted = 4096
    elif target_chapter_count <= 120:
        wanted = 8192
    else:
        wanted = 12000

    limit = max_output_tokens_limit(provider, model)
    if isinstance(limit, int) and limit > 0:
        wanted = min(wanted, int(limit))

    if isinstance(current_max_tokens, int) and current_max_tokens >= wanted:
        return None
    return wanted if wanted > 0 else None


@router.get("/projects/{project_id}/outline")
def get_outline(request: Request, db: DbDep, user_id: UserIdDep, project_id: str) -> dict:
    request_id = request.state.request_id
    project = require_project_viewer(db, project_id=project_id, user_id=user_id)
    row = db.get(Outline, project.active_outline_id) if project.active_outline_id else None
    if row is None:
        row = (
            db.execute(select(Outline).where(Outline.project_id == project_id).order_by(Outline.updated_at.desc()).limit(1))
            .scalars()
            .first()
        )
    if row is None:
        row = ensure_active_outline(db, project=project)
    structure = None
    if row.structure_json:
        try:
            structure = json.loads(row.structure_json)
        except Exception:
            structure = None
    payload = OutlineOut(
        id=row.id,
        project_id=row.project_id,
        title=row.title,
        content_md=row.content_md or "",
        structure=structure,
        created_at=row.created_at,
        updated_at=row.updated_at,
    ).model_dump()
    return ok_payload(request_id=request_id, data={"outline": payload})


@router.put("/projects/{project_id}/outline")
def put_outline(request: Request, db: DbDep, user_id: UserIdDep, project_id: str, body: OutlineUpdate) -> dict:
    request_id = request.state.request_id
    project = require_project_editor(db, project_id=project_id, user_id=user_id)
    row = ensure_active_outline(db, project=project)

    if body.title is not None:
        row.title = body.title
    if body.content_md is not None:
        row.content_md = body.content_md
    if body.structure is not None:
        row.structure_json = json.dumps(body.structure, ensure_ascii=False)

    _mark_vector_index_dirty(db, project_id=project_id)
    db.commit()
    db.refresh(row)
    schedule_vector_rebuild_task(db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="outline_update")
    schedule_search_rebuild_task(db=db, project_id=project_id, actor_user_id=user_id, request_id=request_id, reason="outline_update")
    structure = None
    if row.structure_json:
        try:
            structure = json.loads(row.structure_json)
        except Exception:
            structure = None
    payload = OutlineOut(
        id=row.id,
        project_id=row.project_id,
        title=row.title,
        content_md=row.content_md or "",
        structure=structure,
        created_at=row.created_at,
        updated_at=row.updated_at,
    ).model_dump()
    return ok_payload(request_id=request_id, data={"outline": payload})


@router.post("/projects/{project_id}/outline/generate")
def generate_outline(
    request: Request,
    project_id: str,
    body: OutlineGenerateRequest,
    user_id: UserIdDep,
    x_llm_provider: str | None = Header(default=None, alias="X-LLM-Provider", max_length=64),
    x_llm_api_key: str | None = Header(default=None, alias="X-LLM-API-Key", max_length=4096),
) -> dict:
    request_id = request.state.request_id
    resolved_api_key = ""

    prompt_system = ""
    prompt_user = ""
    prompt_render_log_json: str | None = None
    llm_call = None

    db = SessionLocal()
    try:
        project = require_project_editor(db, project_id=project_id, user_id=user_id)
        preset = db.get(LLMPreset, project_id)
        if preset is None:
            raise AppError(code="LLM_CONFIG_ERROR", message="请先在 Prompts 页保存 LLM 配置", status_code=400)
        if x_llm_api_key and x_llm_provider and preset.provider != x_llm_provider:
            raise AppError(code="LLM_CONFIG_ERROR", message="当前项目 provider 与请求头不一致，请先保存/切换", status_code=400)
        resolved_api_key = resolve_api_key_for_project(db, project=project, user_id=user_id, header_api_key=x_llm_api_key)

        settings_row = db.get(ProjectSettings, project_id)
        world_setting = (settings_row.world_setting if settings_row else "") or ""
        settings_style_guide = (settings_row.style_guide if settings_row else "") or ""
        constraints = (settings_row.constraints if settings_row else "") or ""

        style_resolution: dict[str, object] = {"style_id": None, "source": "disabled"}
        if not body.context.include_world_setting:
            world_setting = ""
            settings_style_guide = ""
            constraints = ""
        else:
            resolved_style_guide, style_resolution = resolve_style_guide(
                db,
                project_id=project_id,
                user_id=user_id,
                requested_style_id=body.style_id,
                include_style_guide=True,
                settings_style_guide=settings_style_guide,
            )
            settings_style_guide = resolved_style_guide

        run_params_extra_json: dict[str, object] = {"style_resolution": style_resolution}

        chars: list[Character] = []
        if body.context.include_characters:
            chars = db.execute(select(Character).where(Character.project_id == project_id)).scalars().all()
        characters_text = format_characters(chars)
        target_chapter_count = _extract_target_chapter_count(body.requirements)
        guidance = _build_outline_generation_guidance(target_chapter_count)

        requirements_text = json.dumps(body.requirements or {}, ensure_ascii=False, indent=2)

        values = {
            "project_name": project.name or "",
            "genre": project.genre or "",
            "logline": project.logline or "",
            "world_setting": world_setting,
            "style_guide": settings_style_guide,
            "constraints": constraints,
            "characters": characters_text,
            "outline": "",
            "chapter_number": "",
            "chapter_title": "",
            "chapter_plan": "",
            "requirements": requirements_text,
            "instruction": "",
            "previous_chapter": "",
            "target_chapter_count": target_chapter_count or "",
            "chapter_count_rule": guidance.get("chapter_count_rule", ""),
            "chapter_detail_rule": guidance.get("chapter_detail_rule", ""),
        }

        prompt_system, prompt_user, prompt_messages, _, _, _, render_log = render_preset_for_task(
            db,
            project_id=project_id,
            task="outline_generate",
            values=values,
            macro_seed=request_id,
            provider=preset.provider,
        )
        prompt_render_log_json = json.dumps(render_log, ensure_ascii=False)

        llm_call = prepare_llm_call(preset)
        current_max_tokens = llm_call.params.get("max_tokens")
        current_max_tokens_int = int(current_max_tokens) if isinstance(current_max_tokens, int) else None
        wanted_max_tokens = _recommend_outline_max_tokens(
            target_chapter_count=target_chapter_count,
            provider=llm_call.provider,
            model=llm_call.model,
            current_max_tokens=current_max_tokens_int,
        )
        if isinstance(wanted_max_tokens, int) and wanted_max_tokens > 0:
            llm_call = with_param_overrides(llm_call, {"max_tokens": wanted_max_tokens})
            run_params_extra_json["outline_auto_max_tokens"] = {
                "target_chapter_count": target_chapter_count,
                "from": current_max_tokens_int,
                "to": wanted_max_tokens,
            }
    finally:
        db.close()

    if llm_call is None:
        raise AppError(code="INTERNAL_ERROR", message="LLM 调用准备失败", status_code=500)

    llm_result = call_llm_and_record(
        logger=logger,
        request_id=request_id,
        actor_user_id=user_id,
        project_id=project_id,
        chapter_id=None,
        run_type="outline",
        api_key=str(resolved_api_key),
        prompt_system=prompt_system,
        prompt_user=prompt_user,
        prompt_messages=prompt_messages,
        prompt_render_log_json=prompt_render_log_json,
        llm_call=llm_call,
        run_params_extra_json=run_params_extra_json,
    )

    raw_output = llm_result.text
    finish_reason = llm_result.finish_reason
    contract = contract_for_task("outline_generate")
    parsed = contract.parse(raw_output, finish_reason=finish_reason)
    data, warnings, parse_error = parsed.data, parsed.warnings, parsed.parse_error

    if parse_error is not None and llm_call.provider in (
        "openai",
        "openai_responses",
        "openai_compatible",
        "openai_responses_compatible",
    ):
        try:
            repair = build_repair_prompt_for_task("outline_generate", raw_output=raw_output)
            if repair is None:
                raise AppError(code="OUTLINE_FIX_UNSUPPORTED", message="该任务不支持输出修复", status_code=400)
            fix_system, fix_user, fix_run_type = repair
            fix_call = with_param_overrides(llm_call, {"temperature": 0, "max_tokens": 1024})
            fixed = call_llm_and_record(
                logger=logger,
                request_id=request_id,
                actor_user_id=user_id,
                project_id=project_id,
                chapter_id=None,
                run_type=fix_run_type,
                api_key=str(resolved_api_key),
                prompt_system=fix_system,
                prompt_user=fix_user,
                llm_call=fix_call,
                run_params_extra_json=run_params_extra_json,
            )
            fixed_parsed = contract.parse(fixed.text)
            fixed_data, fixed_warnings, fixed_error = fixed_parsed.data, fixed_parsed.warnings, fixed_parsed.parse_error
            if fixed_error is None and fixed_data.get("chapters"):
                fixed_data["raw_output"] = raw_output
                fixed_data["fixed_json"] = fixed_data.get("raw_json") or fixed.text
                data = fixed_data
                warnings.extend(["json_fixed_via_llm", *fixed_warnings])
                parse_error = None
        except AppError:
            warnings.append("outline_fix_json_failed")

    if warnings:
        data["warnings"] = warnings
    if parse_error is not None:
        data["parse_error"] = parse_error
    data["generation_run_id"] = llm_result.run_id
    data["latency_ms"] = llm_result.latency_ms
    if llm_result.dropped_params:
        data["dropped_params"] = llm_result.dropped_params
    if finish_reason is not None:
        data["finish_reason"] = finish_reason
    return ok_payload(request_id=request_id, data=data)


@router.post("/projects/{project_id}/outline/generate-stream")
def generate_outline_stream(
    request: Request,
    project_id: str,
    body: OutlineGenerateRequest,
    user_id: UserIdDep,
    x_llm_provider: str | None = Header(default=None, alias="X-LLM-Provider", max_length=64),
    x_llm_api_key: str | None = Header(default=None, alias="X-LLM-API-Key", max_length=4096),
):
    request_id = request.state.request_id

    def event_generator():
        yield sse_progress(message="准备生成...", progress=0)

        prompt_system = ""
        prompt_user = ""
        prompt_render_log_json: str | None = None
        run_params_extra_json: dict[str, object] | None = None
        run_params_json: str | None = None
        llm_call = None
        resolved_api_key = ""

        db = SessionLocal()
        try:
            project = require_project_editor(db, project_id=project_id, user_id=user_id)
            preset = db.get(LLMPreset, project_id)
            if preset is None:
                raise AppError(code="LLM_CONFIG_ERROR", message="请先在 Prompts 页保存 LLM 配置", status_code=400)
            if x_llm_api_key and x_llm_provider and preset.provider != x_llm_provider:
                raise AppError(code="LLM_CONFIG_ERROR", message="当前项目 provider 与请求头不一致，请先保存/切换", status_code=400)
            resolved_api_key = resolve_api_key_for_project(db, project=project, user_id=user_id, header_api_key=x_llm_api_key)

            settings_row = db.get(ProjectSettings, project_id)
            world_setting = (settings_row.world_setting if settings_row else "") or ""
            settings_style_guide = (settings_row.style_guide if settings_row else "") or ""
            constraints = (settings_row.constraints if settings_row else "") or ""

            style_resolution: dict[str, object] = {"style_id": None, "source": "disabled"}
            if not body.context.include_world_setting:
                world_setting = ""
                settings_style_guide = ""
                constraints = ""
            else:
                resolved_style_guide, style_resolution = resolve_style_guide(
                    db,
                    project_id=project_id,
                    user_id=user_id,
                    requested_style_id=body.style_id,
                    include_style_guide=True,
                    settings_style_guide=settings_style_guide,
                )
                settings_style_guide = resolved_style_guide

            run_params_extra_json = {"style_resolution": style_resolution}

            chars: list[Character] = []
            if body.context.include_characters:
                chars = db.execute(select(Character).where(Character.project_id == project_id)).scalars().all()
            characters_text = format_characters(chars)
            target_chapter_count = _extract_target_chapter_count(body.requirements)
            guidance = _build_outline_generation_guidance(target_chapter_count)

            requirements_text = json.dumps(body.requirements or {}, ensure_ascii=False, indent=2)
            values = {
                "project_name": project.name or "",
                "genre": project.genre or "",
                "logline": project.logline or "",
                "world_setting": world_setting,
                "style_guide": settings_style_guide,
                "constraints": constraints,
                "characters": characters_text,
                "outline": "",
                "chapter_number": "",
                "chapter_title": "",
                "chapter_plan": "",
                "requirements": requirements_text,
                "instruction": "",
                "previous_chapter": "",
                "target_chapter_count": target_chapter_count or "",
                "chapter_count_rule": guidance.get("chapter_count_rule", ""),
                "chapter_detail_rule": guidance.get("chapter_detail_rule", ""),
            }

            prompt_system, prompt_user, prompt_messages, _, _, _, render_log = render_preset_for_task(
                db,
                project_id=project_id,
                task="outline_generate",
                values=values,
                macro_seed=request_id,
                provider=preset.provider,
            )
            prompt_render_log_json = json.dumps(render_log, ensure_ascii=False)
            llm_call = prepare_llm_call(preset)
            current_max_tokens = llm_call.params.get("max_tokens")
            current_max_tokens_int = int(current_max_tokens) if isinstance(current_max_tokens, int) else None
            wanted_max_tokens = _recommend_outline_max_tokens(
                target_chapter_count=target_chapter_count,
                provider=llm_call.provider,
                model=llm_call.model,
                current_max_tokens=current_max_tokens_int,
            )
            if isinstance(wanted_max_tokens, int) and wanted_max_tokens > 0:
                llm_call = with_param_overrides(llm_call, {"max_tokens": wanted_max_tokens})
                run_params_extra_json["outline_auto_max_tokens"] = {
                    "target_chapter_count": target_chapter_count,
                    "from": current_max_tokens_int,
                    "to": wanted_max_tokens,
                }
            run_params_json = build_run_params_json(
                params_json=llm_call.params_json,
                memory_retrieval_log_json=None,
                extra_json=run_params_extra_json,
            )
        except GeneratorExit:
            return
        except AppError as exc:
            yield sse_error(error=f"{exc.message} ({exc.code})", code=exc.status_code)
            yield sse_done()
            return
        finally:
            db.close()

        if llm_call is None:
            yield sse_error(error="LLM 调用准备失败", code=500)
            yield sse_done()
            return
        if run_params_json is None:
            run_params_json = build_run_params_json(
                params_json=llm_call.params_json,
                memory_retrieval_log_json=None,
                extra_json=run_params_extra_json,
            )

        yield sse_progress(message="调用模型...", progress=10)

        raw_output = ""
        generation_run_id: str | None = None
        finish_reason: str | None = None
        dropped_params: list[str] = []
        latency_ms: int | None = None
        stream_run_written = False

        try:
            stream_iter, state = call_llm_stream_messages(
                provider=llm_call.provider,
                base_url=llm_call.base_url,
                model=llm_call.model,
                api_key=str(resolved_api_key),
                messages=prompt_messages,
                params=llm_call.params,
                timeout_seconds=llm_call.timeout_seconds,
                extra=llm_call.extra,
            )

            last_progress = 10
            last_progress_ts = 0.0
            chunk_count = 0
            try:
                for delta in stream_iter:
                    raw_output += delta
                    yield sse_chunk(delta)
                    chunk_count += 1
                    if chunk_count % 12 == 0:
                        yield sse_heartbeat()
                    now = time.monotonic()
                    if now - last_progress_ts >= 0.8:
                        next_progress = 10 + int(min(1.0, len(raw_output) / 6000.0) * 80)
                        next_progress = max(last_progress, min(90, next_progress))
                        if next_progress != last_progress:
                            last_progress = next_progress
                            yield sse_progress(message="生成中...", progress=next_progress)
                        last_progress_ts = now
            finally:
                close = getattr(stream_iter, "close", None)
                if callable(close):
                    close()

            finish_reason = state.finish_reason
            dropped_params = state.dropped_params
            latency_ms = state.latency_ms

            log_event(
                logger,
                "info",
                llm={
                    "provider": llm_call.provider,
                    "model": llm_call.model,
                    "timeout_seconds": llm_call.timeout_seconds,
                    "prompt_chars": len(prompt_system) + len(prompt_user),
                    "output_chars": len(raw_output or ""),
                    "dropped_params": dropped_params,
                    "finish_reason": finish_reason,
                    "stream": True,
                },
            )
            generation_run_id = write_generation_run(
                request_id=request_id,
                actor_user_id=user_id,
                project_id=project_id,
                chapter_id=None,
                run_type="outline_stream",
                provider=llm_call.provider,
                model=llm_call.model,
                prompt_system=prompt_system,
                prompt_user=prompt_user,
                prompt_render_log_json=prompt_render_log_json,
                params_json=run_params_json,
                output_text=raw_output,
                error_json=None,
            )
            stream_run_written = True

            yield sse_progress(message="解析输出...", progress=90)
            contract = contract_for_task("outline_generate")
            parsed = contract.parse(raw_output, finish_reason=finish_reason)
            data, warnings, parse_error = parsed.data, parsed.warnings, parsed.parse_error

            if parse_error is not None and llm_call.provider in (
                "openai",
                "openai_responses",
                "openai_compatible",
                "openai_responses_compatible",
            ):
                yield sse_progress(message="尝试修复 JSON...", progress=92)
                repair = build_repair_prompt_for_task("outline_generate", raw_output=raw_output)
                if repair is None:
                    warnings.append("outline_fix_json_failed")
                    repair = None
                if repair is None:
                    raise AppError(code="OUTLINE_FIX_UNSUPPORTED", message="该任务不支持输出修复", status_code=400)
                fix_system, fix_user, fix_run_type = repair
                fix_call = with_param_overrides(llm_call, {"temperature": 0, "max_tokens": 1024})
                try:
                    fixed = call_llm_and_record(
                        logger=logger,
                        request_id=request_id,
                        actor_user_id=user_id,
                        project_id=project_id,
                        chapter_id=None,
                        run_type=fix_run_type,
                        api_key=str(resolved_api_key),
                        prompt_system=fix_system,
                        prompt_user=fix_user,
                        llm_call=fix_call,
                        run_params_extra_json=run_params_extra_json,
                    )
                    fixed_parsed = contract.parse(fixed.text)
                    fixed_data, fixed_warnings, fixed_error = fixed_parsed.data, fixed_parsed.warnings, fixed_parsed.parse_error
                    if fixed_error is None and fixed_data.get("chapters"):
                        fixed_data["raw_output"] = raw_output
                        fixed_data["fixed_json"] = fixed_data.get("raw_json") or fixed.text
                        data = fixed_data
                        warnings.extend(["json_fixed_via_llm", *fixed_warnings])
                        parse_error = None
                except AppError:
                    warnings.append("outline_fix_json_failed")

            if warnings:
                data["warnings"] = warnings
            if parse_error is not None:
                data["parse_error"] = parse_error
            if finish_reason is not None:
                data["finish_reason"] = finish_reason
            if latency_ms is not None:
                data["latency_ms"] = latency_ms
            if dropped_params:
                data["dropped_params"] = dropped_params
            if generation_run_id is not None:
                data["generation_run_id"] = generation_run_id

            yield sse_progress(message="完成", progress=100, status="success")
            yield sse_result(data)
            yield sse_done()
        except GeneratorExit:
            return
        except AppError as exc:
            if (
                llm_call is not None
                and not stream_run_written
            ):
                write_generation_run(
                    request_id=request_id,
                    actor_user_id=user_id,
                    project_id=project_id,
                    chapter_id=None,
                    run_type="outline_stream",
                    provider=llm_call.provider,
                    model=llm_call.model,
                    prompt_system=prompt_system,
                    prompt_user=prompt_user,
                    prompt_render_log_json=prompt_render_log_json,
                    params_json=run_params_json,
                    output_text=raw_output or None,
                    error_json=json.dumps({"code": exc.code, "message": exc.message, "details": exc.details}, ensure_ascii=False),
                )
                stream_run_written = True
            yield sse_error(error=f"{exc.message} ({exc.code})", code=exc.status_code)
            yield sse_done()
        except Exception:
            if llm_call is not None and not stream_run_written:
                write_generation_run(
                    request_id=request_id,
                    actor_user_id=user_id,
                    project_id=project_id,
                    chapter_id=None,
                    run_type="outline_stream",
                    provider=llm_call.provider,
                    model=llm_call.model,
                    prompt_system=prompt_system,
                    prompt_user=prompt_user,
                    prompt_render_log_json=prompt_render_log_json,
                    params_json=run_params_json,
                    output_text=raw_output or None,
                    error_json=json.dumps({"code": "INTERNAL_ERROR", "message": "服务器内部错误"}, ensure_ascii=False),
                )
                stream_run_written = True
            yield sse_error(error="服务器内部错误", code=500)
            yield sse_done()

    return create_sse_response(event_generator())
