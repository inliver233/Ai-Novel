"""backend-api#7：SSE/生成端点在阻塞 LLM 调用前必须结束读事务（短会话收敛）。

请求级 Session 在鉴权/取数后处于打开的读事务中；直接进入分钟级 LLM 调用会
造成 idle-in-transaction 并占住连接池连接。终审语义：commit 收敛读事务即可，
不做 async 化。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.detailed_outline import DetailedOutline
from app.models.outline import Outline
from app.models.project import Project
from app.models.user import User


def _factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _seed(db: Session) -> None:
    db.add(User(id="u1", display_name="user"))
    db.add(Project(id="p1", owner_user_id="u1", name="P1", active_outline_id="o1"))
    db.add(Outline(id="o1", project_id="p1", title="O1", content_md="# 大纲"))
    db.add(
        DetailedOutline(
            id="do-1",
            project_id="p1",
            outline_id="o1",
            volume_number=1,
            volume_title="卷一",
            content_md="beats",
            structure_json=None,
            status="planned",
        )
    )
    db.commit()


class _FakeLlmResult:
    text = '{"chapters": [{"number": 1, "title": "第一章"}]}'
    run_id = "run-1"
    latency_ms = 1
    finish_reason = "stop"
    dropped_params: list[str] = []


class _FakeStreamState:
    finish_reason = "stop"
    latency_ms = 1
    dropped_params: list[str] = []


def test_detailed_outline_generation_ends_read_transaction_before_llm() -> None:
    from app.services.detailed_outline_generation import app_service
    from app.services.detailed_outline_generation.models import VolumeInfo
    from app.services.generation_service import PreparedLlmCall

    factory = _factory()
    in_transaction: list[bool] = []

    def _fake_call(**kwargs: Any) -> _FakeLlmResult:
        in_transaction.append(probe_db.in_transaction())
        return _FakeLlmResult()

    with factory() as probe_db:
        _seed(probe_db)
        outline = probe_db.get(Outline, "o1")
        project = probe_db.get(Project, "p1")
        # 模拟请求级会话：鉴权查询已打开读事务。
        assert probe_db.in_transaction()

        llm_call = PreparedLlmCall(
            provider="openai",
            base_url="http://example",
            model="gpt-x",
            params={},
            params_json="{}",
            timeout_seconds=10,
            extra={},
        )
        with (
            patch.object(
                app_service,
                "render_preset_for_task",
                return_value=("sys", "user", [], None, None, None, {}),
            ),
            patch.object(app_service, "call_llm_and_record", side_effect=_fake_call),
        ):
            app_service.generate_detailed_outline_for_volume(
                outline,
                VolumeInfo(number=1, title="卷一", beats_text="beats", chapter_range_start=1, chapter_range_end=0),
                project,
                llm_call,
                "api-key",
                "rid-1",
                "u1",
                probe_db,
            )

    assert in_transaction == [False], "阻塞 LLM 调用前必须 commit 收敛读事务"


def test_chapter_skeleton_stream_ends_read_transaction_before_llm() -> None:
    from app.services.chapter_skeleton_generation import stream_service
    from app.services.generation_service import PreparedLlmCall

    factory = _factory()
    in_transaction: list[bool] = []

    def _fake_stream_call(**kwargs: Any) -> tuple[Any, _FakeStreamState]:
        in_transaction.append(probe_db.in_transaction())
        chunks = iter(['{"chapters":[{"number":1,"title":"第一章"}]}'])
        return chunks, _FakeStreamState()

    with factory() as probe_db:
        _seed(probe_db)
        detailed_outline = probe_db.get(DetailedOutline, "do-1")
        outline = probe_db.get(Outline, "o1")
        project = probe_db.get(Project, "p1")
        assert detailed_outline is not None
        assert outline is not None
        assert project is not None
        # 模拟路由鉴权和邻卷查询留下的请求级读事务。
        assert probe_db.in_transaction()

        llm_call = PreparedLlmCall(
            provider="openai",
            base_url="http://example",
            model="gpt-x",
            params={},
            params_json="{}",
            timeout_seconds=10,
            extra={},
        )
        with (
            patch.object(
                stream_service,
                "render_preset_for_task",
                return_value=("sys", "user", [], None, None, None, {}),
            ),
            patch.object(stream_service, "call_llm_stream_messages", side_effect=_fake_stream_call),
            patch.object(stream_service, "write_generation_run", return_value="run-1"),
        ):
            list(
                stream_service.generate_chapter_skeleton_stream_events(
                    request_id="rid-1",
                    detailed_outline=detailed_outline,
                    outline=outline,
                    project=project,
                    llm_call=llm_call,
                    api_key="api-key",
                    user_id="u1",
                    db=probe_db,
                )
            )

    assert in_transaction == [False], "流式 LLM 调用前必须 commit 收敛读事务"
