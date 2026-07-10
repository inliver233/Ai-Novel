"""generation pipeline 共用 rewrite step 的配置降级与任务差异回归测试。

LLM 配置解析失败时，post-edit/content-optimize 保持既有 fail-soft fallback，
同时向调用方返回稳定 warning，并记录不含配置值的 WARNING 日志。两条公开入口
仍须保留各自的温度、run type、sanitize 渲染和结果字段契约。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from app.models.llm_preset import LLMPreset
from app.models.project import Project
from app.services.generation_pipeline import run_content_optimize_step, run_post_edit_step
from app.services.generation_service import PreparedLlmCall
from tests.support import (
    create_tables,
    make_recorded_result,
    make_session_factory,
    make_sqlite_engine,
    patch_call_llm,
    seed_user,
)

USER_ID = "u1"
PROJECT_ID = "p1"


def _make_fallback_llm_call() -> PreparedLlmCall:
    """构造一个最小有效的 fallback PreparedLlmCall（供 pipeline 在 resolved=None 时使用）。"""
    return PreparedLlmCall(
        provider="openai",
        model="gpt-4",
        base_url="",
        timeout_seconds=30,
        params={
            "temperature": 0.4,
            "top_p": None,
            "max_tokens": None,
            "presence_penalty": None,
            "frequency_penalty": None,
            "top_k": None,
            "stop": [],
        },
        params_json="{}",
        extra={},
    )


@pytest.fixture
def env():
    """每个用例独立的内存 DB + 坏 LLM 配置 seed 数据。"""
    # M18：project 无 llm_profile_id → resolve_task_llm_config 因
    # "无 profile 且无 header API key" 抛 AppError("LLM_KEY_MISSING")
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine)
    seed_user(factory, user_id=USER_ID)

    with factory() as db:
        db.add(Project(id=PROJECT_ID, owner_user_id=USER_ID, name="测试项目"))
        # LLMPreset 行必须存在，否则 resolve_task_preset 返回 None →
        # resolve_task_llm_config 直接返回 None（不抛异常，不触发 bug）
        db.add(LLMPreset(project_id=PROJECT_ID, provider="openai", model="gpt-4"))
        db.commit()

    yield {"factory": factory}
    engine.dispose()


def test_post_edit_surfaces_llm_config_error(env, caplog: pytest.LogCaptureFixture):
    """post-edit 配置失败可观察，且继续使用调用方 fallback。"""
    factory = env["factory"]
    edited_content = "润色后的章节内容保持情节与人物关系完整。" * 6
    fake_result = make_recorded_result(text=f"<rewrite>{edited_content}</rewrite>")
    with caplog.at_level(logging.WARNING, logger="test"):
        caplog.clear()
        with patch("app.services.generation_pipeline.SessionLocal", factory):
            with patch_call_llm(
                fake_result, path="app.services.generation_pipeline.call_llm_and_record"
            ) as call_mock:
                result = run_post_edit_step(
                    logger=logging.getLogger("test"),
                    request_id="rid-test",
                    actor_user_id=USER_ID,
                    project_id=PROJECT_ID,
                    chapter_id=None,
                    api_key="fallback-key",
                    llm_call=_make_fallback_llm_call(),
                    render_values={},
                    raw_content="原始章节内容",
                    macro_seed="seed",
                )

    assert result.applied is True
    assert result.edited_content_md == edited_content
    assert "llm_config_resolve_failed" in result.warnings
    call_kwargs = call_mock.call_args.kwargs
    assert call_kwargs["api_key"] == "fallback-key"
    assert call_kwargs["run_type"] == "post_edit"
    assert call_kwargs["llm_call"].params["temperature"] == 0.4

    warning_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "test" and record.levelno == logging.WARNING
    ]
    assert warning_messages == ["llm_config_resolve_failed task=post_edit error_type=AppError"]
    assert "fallback-key" not in warning_messages[0]


def test_content_optimize_surfaces_llm_config_error(env, caplog: pytest.LogCaptureFixture):
    """content-optimize 配置失败可观察，且保留自身调用元数据。"""
    factory = env["factory"]
    optimized_content = "优化后的章节内容保持情节与人物关系完整。" * 6
    fake_result = make_recorded_result(text=f"<content>{optimized_content}</content>")
    with caplog.at_level(logging.WARNING, logger="test"):
        caplog.clear()
        with patch("app.services.generation_pipeline.SessionLocal", factory):
            with patch_call_llm(
                fake_result, path="app.services.generation_pipeline.call_llm_and_record"
            ) as call_mock:
                result = run_content_optimize_step(
                    logger=logging.getLogger("test"),
                    request_id="rid-test",
                    actor_user_id=USER_ID,
                    project_id=PROJECT_ID,
                    chapter_id=None,
                    api_key="fallback-key",
                    llm_call=_make_fallback_llm_call(),
                    render_values={},
                    raw_content="原始章节内容",
                    macro_seed="seed",
                )

    assert result.applied is True
    assert result.optimized_content_md == optimized_content
    assert "llm_config_resolve_failed" in result.warnings
    call_kwargs = call_mock.call_args.kwargs
    assert call_kwargs["api_key"] == "fallback-key"
    assert call_kwargs["run_type"] == "content_optimize"
    assert call_kwargs["llm_call"].params["temperature"] == 0.35

    warning_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "test" and record.levelno == logging.WARNING
    ]
    assert warning_messages == ["llm_config_resolve_failed task=content_optimize error_type=AppError"]
    assert "fallback-key" not in warning_messages[0]


def test_post_edit_sanitize_preserves_render_and_run_metadata(env, caplog: pytest.LogCaptureFixture):
    """resolver 正常返回 None 时不告警，sanitize 差异仍传入共享主流程。"""
    factory = env["factory"]
    edited_content = "深度净化后的章节内容保持情节与人物关系完整。" * 6
    fake_result = make_recorded_result(text=f"<rewrite>{edited_content}</rewrite>")

    with caplog.at_level(logging.WARNING, logger="test"):
        caplog.clear()
        with patch("app.services.generation_pipeline.SessionLocal", factory):
            with patch("app.services.generation_pipeline.resolve_task_llm_config", return_value=None):
                with patch_call_llm(
                    fake_result,
                    path="app.services.generation_pipeline.call_llm_and_record",
                ) as call_mock:
                    result = run_post_edit_step(
                        logger=logging.getLogger("test"),
                        request_id="rid-test",
                        actor_user_id=USER_ID,
                        project_id=PROJECT_ID,
                        chapter_id=None,
                        api_key="fallback-key",
                        llm_call=_make_fallback_llm_call(),
                        render_values={},
                        raw_content="原始章节内容",
                        macro_seed="seed",
                        post_edit_sanitize=True,
                    )

    assert result.applied is True
    assert "llm_config_resolve_failed" not in result.warnings
    assert not caplog.records

    call_kwargs = call_mock.call_args.kwargs
    assert call_kwargs["run_type"] == "post_edit_sanitize"
    assert call_kwargs["llm_call"].params["temperature"] == 0.4
    rendered_prompt = "\n".join(
        [
            str(call_kwargs["prompt_system"]),
            str(call_kwargs["prompt_user"]),
            str(call_kwargs["prompt_messages"]),
        ]
    )
    assert "<DEEP_SANITIZE>" in rendered_prompt


def test_resolved_task_config_replaces_fallback_call_and_key(env, caplog: pytest.LogCaptureFixture):
    """成功解析任务配置时，共享主流程同时替换 call 与 key 且不记录降级。"""
    factory = env["factory"]
    fallback_call = _make_fallback_llm_call()
    resolved_call = PreparedLlmCall(
        provider="openai",
        model="resolved-model",
        base_url="https://resolved.example/v1",
        timeout_seconds=45,
        params={**fallback_call.params, "temperature": 0.9},
        params_json="{}",
        extra={},
    )
    edited_content = "使用任务级配置完成的章节润色内容。" * 8
    fake_result = make_recorded_result(text=f"<rewrite>{edited_content}</rewrite>")

    with caplog.at_level(logging.WARNING, logger="test"):
        caplog.clear()
        with patch("app.services.generation_pipeline.SessionLocal", factory):
            with patch(
                "app.services.generation_pipeline.resolve_task_llm_config",
                return_value=SimpleNamespace(llm_call=resolved_call, api_key="resolved-key"),
            ):
                with patch_call_llm(
                    fake_result,
                    path="app.services.generation_pipeline.call_llm_and_record",
                ) as call_mock:
                    result = run_post_edit_step(
                        logger=logging.getLogger("test"),
                        request_id="rid-test",
                        actor_user_id=USER_ID,
                        project_id=PROJECT_ID,
                        chapter_id=None,
                        api_key="fallback-key",
                        llm_call=fallback_call,
                        render_values={},
                        raw_content="原始章节内容",
                        macro_seed="seed",
                    )

    assert result.applied is True
    assert "llm_config_resolve_failed" not in result.warnings
    assert not caplog.records
    call_kwargs = call_mock.call_args.kwargs
    assert call_kwargs["api_key"] == "resolved-key"
    assert call_kwargs["llm_call"].model == "resolved-model"
    assert call_kwargs["llm_call"].base_url == "https://resolved.example/v1"
    assert call_kwargs["llm_call"].params["temperature"] == 0.4


def test_rewrite_warning_order_preserves_config_parse_validation_and_failure(env):
    """共享主流程按可诊断顺序合并不同阶段的 warning。"""
    factory = env["factory"]
    fake_result = make_recorded_result(text="prefix <rewrite>short</rewrite>")

    with patch("app.services.generation_pipeline.SessionLocal", factory):
        with patch_call_llm(
            fake_result,
            path="app.services.generation_pipeline.call_llm_and_record",
        ):
            result = run_post_edit_step(
                logger=logging.getLogger("test"),
                request_id="rid-test",
                actor_user_id=USER_ID,
                project_id=PROJECT_ID,
                chapter_id=None,
                api_key="fallback-key",
                llm_call=_make_fallback_llm_call(),
                render_values={},
                raw_content="a" * 600,
                macro_seed="seed",
            )

    assert result.applied is False
    assert result.edited_content_md == "short"
    assert result.warnings == [
        "llm_config_resolve_failed",
        "tag_outside_text",
        "post_edit_too_short",
        "post_edit_failed",
    ]
