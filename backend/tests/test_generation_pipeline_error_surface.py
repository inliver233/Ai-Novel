"""B 类 known_issue 测试 —— generation_pipeline post_edit/content_optimize
静默吞掉 LLM 配置错误（catalog M18）。

【诚实镜像】断言【正确行为】：LLM 配置错误（无 profile 且无 header API key）
可以按既有 fail-soft 设计继续使用调用方传入的 fallback 配置，但必须在 step.warnings
写入结构化告警，并以 WARNING 级别记录降级，使调用方/用户与运维日志都能观察到；
不能被 try/except 静默吞掉。
当前实现（app/services/generation_pipeline.py:127,214）的 ``except Exception``
捕获了 ``resolve_task_llm_config`` 抛出的 ``AppError("LLM_KEY_MISSING")`` 并设
``resolved=None``，导致错误被吞、函数继续用 fallback 配置——用户无感知。
对照 ``run_mcp_research_step``（同文件 line 90）有 ``logger.exception``，证明吞错是遗漏。
bug 修复后（记录 WARNING 并追加 ``llm_config_resolve_failed``）→ 测试绿。
"""

from __future__ import annotations

import logging
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


@pytest.mark.known_issue  # M18
def test_post_edit_surfaces_llm_config_error(env, caplog: pytest.LogCaptureFixture):
    """run_post_edit_step 在配置降级时应返回结构化 warning。

    正确行为：project 无 llm_profile_id 且无 header API key 时，
    resolve_task_llm_config 抛 AppError("LLM_KEY_MISSING")；pipeline 可以继续使用
    fallback llm_call，但结果必须包含 ``llm_config_resolve_failed``。
    当前 bug（generation_pipeline.py:127）：except Exception → resolved=None →
    静默继续用 fallback 配置且 warnings 无记录 → 断言失败（红）。
    """
    factory = env["factory"]
    edited_content = "润色后的章节内容保持情节与人物关系完整。" * 6
    fake_result = make_recorded_result(text=f"<rewrite>{edited_content}</rewrite>")
    with caplog.at_level(logging.WARNING, logger="test"):
        caplog.clear()
        with patch("app.services.generation_pipeline.SessionLocal", factory):
            with patch_call_llm(
                fake_result, path="app.services.generation_pipeline.call_llm_and_record"
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
                    raw_content="原始章节内容",
                    macro_seed="seed",
                )

    assert result.applied is True
    assert "llm_config_resolve_failed" in result.warnings
    assert any(record.name == "test" and record.levelno == logging.WARNING for record in caplog.records)


@pytest.mark.known_issue  # M18
def test_content_optimize_surfaces_llm_config_error(env, caplog: pytest.LogCaptureFixture):
    """run_content_optimize_step 在配置降级时应返回结构化 warning。

    正确行为：同 post_edit——允许 fallback，但 warnings 必须包含稳定错误码。
    当前 bug（generation_pipeline.py:214）：except Exception → resolved=None →
    静默继续且无 warning → 断言失败（红）。
    """
    factory = env["factory"]
    optimized_content = "优化后的章节内容保持情节与人物关系完整。" * 6
    fake_result = make_recorded_result(text=f"<content>{optimized_content}</content>")
    with caplog.at_level(logging.WARNING, logger="test"):
        caplog.clear()
        with patch("app.services.generation_pipeline.SessionLocal", factory):
            with patch_call_llm(
                fake_result, path="app.services.generation_pipeline.call_llm_and_record"
            ):
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
    assert "llm_config_resolve_failed" in result.warnings
    assert any(record.name == "test" and record.levelno == logging.WARNING for record in caplog.records)
