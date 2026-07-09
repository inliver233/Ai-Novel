"""generation_runs 域路由 happy-path（D 类安全网）。

覆盖 ``app/api/routes/generation_runs.py`` 的全部 HTTP 端点：

  GET /api/projects/{project_id}/generation_runs
      —— 列出某项目的生成运行记录（支持 limit / chapter_id / request_id 过滤，
         按 created_at 倒序）
  GET /api/generation_runs/{run_id}
      —— 单条运行记录详情（含 params / error / prompt_render_log 的 JSON 解析）
  GET /api/generation_runs/{run_id}/debug_bundle
      —— 下载某次运行的调试包（JSON 附件，聚合 run + prompt + vector rag 状态）

跳过项：无。该路由文件只暴露上述 3 个 GET 只读端点，全部是对**已存在**
GenerationRun 行的查询/详情/打包，不触发任何真实 LLM 生成调用，故全部
纳入 happy-path。debug_bundle 在缺少 memory_injection_config 时走
vector_rag_status（纯配置计算，无向量检索副作用），是安全的只读路径。
"""

from __future__ import annotations

from datetime import timedelta

from app.api.routes import generation_runs as generation_runs_routes
from app.db.utils import utc_now
from app.models.generation_run import GenerationRun
from app.models.project import Project
from app.models.project_membership import ProjectMembership
from app.models.project_settings import ProjectSettings
from app.models.user import User
from app.models.user_password import UserPassword

from tests.support import (
    auth_cookies,
    create_tables,
    make_client,
    make_session_factory,
    make_sqlite_engine,
    make_test_app,
    seed_user,
)


# 这些测试需要建的表：seed_user 需要 User/UserPassword；require_project_viewer
# 经 _project_role 需要 Project/ProjectMembership（owner 直接命中即可）；
# debug_bundle 用 db.get(ProjectSettings, ...)（None 安全，但建表以防外键）。
_MODELS = [
    User,
    UserPassword,
    Project,
    ProjectMembership,
    ProjectSettings,
    GenerationRun,
]


def _new_client_and_factory():
    """构造一个已建表 + 已登录 u1 的测试 client 与 session factory。"""
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, _MODELS)
    seed_user(factory, user_id="u1")
    app = make_test_app(factory, [generation_runs_routes])
    client = make_client(app)
    auth_cookies(client, "u1")
    return client, factory


def _seed_project(factory, *, project_id: str = "p1", owner_user_id: str = "u1") -> str:
    """直接在 DB 插入一个 Project + owner membership，返回 project_id。"""
    with factory() as db:
        db.add(Project(id=project_id, owner_user_id=owner_user_id, name="P1"))
        db.add(ProjectMembership(project_id=project_id, user_id=owner_user_id, role="owner"))
        db.commit()
    return project_id


def _seed_run(
    factory,
    *,
    run_id: str,
    project_id: str = "p1",
    actor_user_id: str = "u1",
    chapter_id: str | None = None,
    type: str = "chapter",
    provider: str | None = "mock",
    model: str | None = "mock-1",
    request_id: str | None = None,
    prompt_system: str | None = None,
    prompt_user: str | None = None,
    prompt_render_log_json: str | None = None,
    params_json: str | None = None,
    output_text: str | None = None,
    error_json: str | None = None,
    created_at=None,
) -> str:
    """直接在 DB 插入一条 GenerationRun，返回 run_id。"""
    with factory() as db:
        db.add(
            GenerationRun(
                id=run_id,
                project_id=project_id,
                actor_user_id=actor_user_id,
                chapter_id=chapter_id,
                type=type,
                provider=provider,
                model=model,
                request_id=request_id,
                prompt_system=prompt_system,
                prompt_user=prompt_user,
                prompt_render_log_json=prompt_render_log_json,
                params_json=params_json,
                output_text=output_text,
                error_json=error_json,
                created_at=created_at if created_at is not None else utc_now(),
            )
        )
        db.commit()
    return run_id


# 结构化断言用的关键键集合（与 GenerationRunOut 对齐，不锁中文文案）。
_RUN_KEYS = {
    "id",
    "project_id",
    "actor_user_id",
    "chapter_id",
    "type",
    "provider",
    "model",
    "request_id",
    "prompt_system",
    "prompt_user",
    "prompt_render_log",
    "params",
    "output_text",
    "error",
    "created_at",
}


# ---------- GET /api/projects/{project_id}/generation_runs ----------


def test_list_runs_empty_returns_ok_with_empty_list() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    resp = client.get("/api/projects/p1/generation_runs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {"runs": []}


def test_list_runs_returns_runs_for_project() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    _seed_run(factory, run_id="r1", project_id="p1", type="chapter", output_text="hello")
    resp = client.get("/api/projects/p1/generation_runs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    runs = body["data"]["runs"]
    assert len(runs) == 1
    run = runs[0]
    assert _RUN_KEYS.issubset(run.keys())
    assert run["id"] == "r1"
    assert run["project_id"] == "p1"
    assert run["type"] == "chapter"
    assert run["output_text"] == "hello"


def test_list_runs_scoped_to_project_only() -> None:
    """list 只返回 path 上 project_id 的 run，不串项目。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    _seed_project(factory, project_id="p2")
    _seed_run(factory, run_id="r1", project_id="p1")
    _seed_run(factory, run_id="r2", project_id="p2")
    resp = client.get("/api/projects/p1/generation_runs")
    assert resp.status_code == 200
    runs = resp.json()["data"]["runs"]
    assert {r["id"] for r in runs} == {"r1"}


def test_list_runs_filters_by_chapter_id() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    _seed_run(factory, run_id="r1", project_id="p1", chapter_id="c1")
    _seed_run(factory, run_id="r2", project_id="p1", chapter_id="c2")
    resp = client.get("/api/projects/p1/generation_runs?chapter_id=c1")
    assert resp.status_code == 200
    runs = resp.json()["data"]["runs"]
    assert len(runs) == 1
    assert runs[0]["id"] == "r1"
    assert runs[0]["chapter_id"] == "c1"


def test_list_runs_filters_by_request_id_alias() -> None:
    """query 参数用 alias ``request_id``（函数签名 alias="request_id"）。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    _seed_run(factory, run_id="r1", project_id="p1", request_id="req-aaa")
    _seed_run(factory, run_id="r2", project_id="p1", request_id="req-bbb")
    resp = client.get("/api/projects/p1/generation_runs?request_id=req-bbb")
    assert resp.status_code == 200
    runs = resp.json()["data"]["runs"]
    assert len(runs) == 1
    assert runs[0]["id"] == "r2"
    assert runs[0]["request_id"] == "req-bbb"


def test_list_runs_respects_limit_param() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    base = utc_now()
    _seed_run(factory, run_id="r1", project_id="p1", created_at=base)
    _seed_run(factory, run_id="r2", project_id="p1", created_at=base + timedelta(seconds=1))
    _seed_run(factory, run_id="r3", project_id="p1", created_at=base + timedelta(seconds=2))
    resp = client.get("/api/projects/p1/generation_runs?limit=2")
    assert resp.status_code == 200
    runs = resp.json()["data"]["runs"]
    assert len(runs) == 2
    # 倒序：最新的两条
    assert [r["id"] for r in runs] == ["r3", "r2"]


def test_list_runs_orders_by_created_at_desc() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    base = utc_now()
    _seed_run(factory, run_id="oldest", project_id="p1", created_at=base)
    _seed_run(factory, run_id="newest", project_id="p1", created_at=base + timedelta(seconds=10))
    _seed_run(factory, run_id="middle", project_id="p1", created_at=base + timedelta(seconds=5))
    resp = client.get("/api/projects/p1/generation_runs?limit=10")
    assert resp.status_code == 200
    runs = resp.json()["data"]["runs"]
    assert [r["id"] for r in runs] == ["newest", "middle", "oldest"]


# ---------- GET /api/generation_runs/{run_id} ----------


def test_get_run_returns_run_detail_with_full_shape() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    _seed_run(
        factory,
        run_id="r1",
        project_id="p1",
        type="chapter",
        provider="mock",
        model="mock-1",
        request_id="req-1",
        prompt_system="sys",
        prompt_user="usr",
        output_text="out",
    )
    resp = client.get("/api/generation_runs/r1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    run = body["data"]["run"]
    assert _RUN_KEYS.issubset(run.keys())
    assert run["id"] == "r1"
    assert run["project_id"] == "p1"
    assert run["actor_user_id"] == "u1"
    assert run["type"] == "chapter"
    assert run["provider"] == "mock"
    assert run["model"] == "mock-1"
    assert run["request_id"] == "req-1"
    assert run["prompt_system"] == "sys"
    assert run["prompt_user"] == "usr"
    assert run["output_text"] == "out"


def test_get_run_parses_params_json_into_dict() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    _seed_run(
        factory,
        run_id="r1",
        project_id="p1",
        params_json='{"temperature": 0.7, "top_p": 0.9}',
    )
    resp = client.get("/api/generation_runs/r1")
    assert resp.status_code == 200
    params = resp.json()["data"]["run"]["params"]
    assert params == {"temperature": 0.7, "top_p": 0.9}


def test_get_run_parses_error_json_into_dict() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    _seed_run(
        factory,
        run_id="r1",
        project_id="p1",
        error_json='{"code": "timeout", "stage": "llm"}',
    )
    resp = client.get("/api/generation_runs/r1")
    assert resp.status_code == 200
    err = resp.json()["data"]["run"]["error"]
    assert err == {"code": "timeout", "stage": "llm"}


def test_get_run_parses_prompt_render_log_json() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    _seed_run(
        factory,
        run_id="r1",
        project_id="p1",
        prompt_render_log_json='{"blocks": 3, "truncated": false}',
    )
    resp = client.get("/api/generation_runs/r1")
    assert resp.status_code == 200
    render_log = resp.json()["data"]["run"]["prompt_render_log"]
    assert render_log == {"blocks": 3, "truncated": False}


def test_get_run_defaults_params_and_error_to_empty_when_absent() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    _seed_run(factory, run_id="r1", project_id="p1")  # 无 params/error/render_log
    resp = client.get("/api/generation_runs/r1")
    assert resp.status_code == 200
    run = resp.json()["data"]["run"]
    assert run["params"] == {}
    assert run["error"] is None
    assert run["prompt_render_log"] is None


# ---------- GET /api/generation_runs/{run_id}/debug_bundle ----------


def test_download_debug_bundle_returns_json_attachment_with_schema() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    _seed_run(
        factory,
        run_id="r1",
        project_id="p1",
        type="chapter",
        provider="mock",
        model="mock-1",
        request_id="req-1",
        prompt_system="sys",
        prompt_user="usr",
        output_text="out",
    )
    resp = client.get("/api/generation_runs/r1/debug_bundle")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    content_disposition = resp.headers.get("content-disposition", "")
    assert "attachment" in content_disposition
    assert "debug_bundle_r1.json" in content_disposition
    bundle = resp.json()
    assert bundle["schema_version"] == "debug_bundle_v1"
    assert bundle["request_id"] == "rid-test"
    run = bundle["run"]
    assert run["id"] == "r1"
    assert run["project_id"] == "p1"
    assert run["type"] == "chapter"
    assert run["provider"] == "mock"
    assert run["model"] == "mock-1"
    assert run["run_request_id"] == "req-1"
    # prompt 块结构键（不锁文案）
    assert set(bundle["prompt"].keys()) >= {"system", "user", "render_log"}
    assert bundle["prompt"]["system"] == "sys"
    assert bundle["prompt"]["user"] == "usr"
    # vector rag 状态被聚合进来（结构键，不锁具体后端状态）
    assert "vector_rag" in bundle
    assert "enabled" in bundle["vector_rag"]


def test_download_debug_bundle_strips_prompt_inspector_by_default() -> None:
    """默认 include_prompt_inspector=0 时，params 里的 prompt_inspector 被剥离。"""
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    _seed_run(
        factory,
        run_id="r1",
        project_id="p1",
        params_json='{"prompt_inspector": {"foo": "bar"}, "keep_me": true}',
    )
    resp = client.get("/api/generation_runs/r1/debug_bundle")
    assert resp.status_code == 200
    params = resp.json()["params"]
    assert "prompt_inspector" not in params
    assert params["keep_me"] is True


def test_download_debug_bundle_keeps_prompt_inspector_when_requested() -> None:
    client, factory = _new_client_and_factory()
    _seed_project(factory, project_id="p1")
    _seed_run(
        factory,
        run_id="r1",
        project_id="p1",
        params_json='{"prompt_inspector": {"foo": "bar"}}',
    )
    resp = client.get("/api/generation_runs/r1/debug_bundle?include_prompt_inspector=1")
    assert resp.status_code == 200
    params = resp.json()["params"]
    assert params["prompt_inspector"] == {"foo": "bar"}
