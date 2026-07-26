"""
==========================================================================
  AI 小说生成平台 — 后端入口文件 (main.py)
==========================================================================

  这是整个后端应用的入口文件，基于 FastAPI 框架构建。
  本文件负责以下核心职责：

  1. 【应用生命周期管理】  lifespan() — 启动/关闭时的初始化与清理
  2. 【中间件注册】         CORS、认证会话、请求 ID 与日志
  3. 【全局异常处理】       AppError / 参数校验 / 数据库 / 未处理异常
  4. 【路由挂载】           将所有 API 路由模块统一挂载到 /api 前缀下

  项目整体架构概览：
  ┌─────────────────────────────────────────────────────────────────┐
  │  frontend (React/Vite, 端口 5173)                              │
  │     ↕ HTTP / JSON                                              │
  │  backend (FastAPI/Uvicorn, 本文件为入口)                         │
  │     ├── app/api/routes/      ← 所有 API 路由 (28 个模块)        │
  │     ├── app/core/            ← 配置/认证/日志/错误 等基础设施     │
  │     ├── app/db/              ← 数据库引擎/会话/迁移 (SQLAlchemy) │
  │     ├── app/models/          ← ORM 数据模型                     │
  │     ├── app/services/        ← 业务逻辑层                       │
  │     ├── app/llm/             ← LLM 大模型调用层                  │
  │     └── alembic/             ← 数据库迁移脚本 (Alembic)          │
  └─────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

# ──────────────────────────────────────────────────────────────────
# 内部模块导入 — 每个导入都对应项目中的一个功能模块
# ──────────────────────────────────────────────────────────────────

# 【总路由】汇总了所有 28 个 API 路由模块，统一挂载到 /api 前缀
#   定义位置：app/api/router.py
#   包含的路由模块（按功能分类）：
#     - 基础：health(健康检查)、auth(认证登录)、settings(系统设置)
#     - 项目：projects(项目CRUD)、export(导出)、import_export(导入导出)
#     - 小说内容：chapters(章节)、characters(角色)、entries(条目)
#     - 大纲：outline(大纲编辑)、outlines(大纲列表)、detailed_outlines(细纲)、outline_parse(大纲解析)
#     - AI 生成：batch_generation(批量生成)、generation_runs(生成运行记录)
#     - LLM 配置：llm(LLM调用)、llm_models(模型列表)、llm_profiles(配置档案)、
#                 llm_preset(预设)、llm_task_presets(任务预设)、llm_capabilities(能力查询)
#     - 提示词：prompts(提示词模板)、prompt_studio(提示词工作室)
#     - 记忆/搜索：memory(记忆管理)、story_memory(故事记忆)、search(搜索)、vector(向量检索)
#     - 其他：mcp(MCP协议)、writing_styles(写作风格)
from app.api.router import api_router

# 【共享引导】数据库迁移、管理员初始化及其应用内执行策略的唯一实现
#   Docker entrypoint 同样通过 `python -m app.bootstrap` 复用这条路径
from app.bootstrap import bootstrap_in_app

# 【全局配置】基于 pydantic-settings 的配置单例，从 .env 文件和环境变量加载
#   定义位置：app/core/config.py
#   包含：数据库URL、CORS、认证、LLM、Redis、向量检索 等所有配置项
#   settings 是全局单例，在模块加载时即实例化
from app.core.config import settings

# 【会话解码】基于 HMAC-SHA256 签名的 Cookie 会话机制
#   定义位置：app/core/auth_session.py
#   decode_session_cookie() 负责从 Cookie 值中解码出 AuthSession(user_id, expires_at)
#   签名密钥经 app/core/key_derivation.py HKDF 域分离派生（主密钥优先级：
#   auth_session_signing_key > secret_encryption_key > 随机生成(仅dev)）
#   Cookie 格式：v3.<base64url_payload>.<base64url_signature>
from app.core.auth_session import clear_session_cookies, decode_session_cookie

# 【统一错误体系】全局业务异常类和标准化错误响应格式
#   定义位置：app/core/errors.py
#   AppError — 业务异常基类，包含 code/message/status_code/details 四个字段
#              提供快捷方法：unauthorized(401)、forbidden(403)、not_found(404)、
#                          conflict(409)、validation(400)
#   error_payload() — 构造标准错误响应体 {"ok": false, "error": {...}, "request_id": "..."}
from app.core.errors import AppError, error_payload, safe_app_error_headers

# 【日志系统】基于 loguru 的结构化 JSON 日志
#   定义位置：app/core/logging.py
#   configure_logging() — 初始化 loguru，拦截 stdlib logging，设置日志级别
#   log_event()          — 输出结构化 JSON 日志（自动附加时间戳和 request_id）
#   exception_log_fields() — 将异常转为安全的日志字段（dev模式显示堆栈，prod模式只显示hash）
#   safe_log_details()   — 过滤日志详情中的敏感字段，只保留白名单内的 key
from app.core.logging import configure_logging, exception_log_fields, log_event, safe_log_details

# 【请求 ID】基于 ContextVar 的请求追踪 ID 机制
#   定义位置：app/core/request_id.py
#   new_request_id()  — 生成 UUID v4 作为请求 ID
#   set_request_id()  — 将请求 ID 存入当前协程的 ContextVar
#   reset_request_id() — 请求结束后重置 ContextVar（防止泄漏到其他请求）
#   get_request_id()  — 在任意位置获取当前请求的 ID（日志系统会自动调用）
from app.core.request_id import new_request_id, reset_request_id, set_request_id

# 【数据库会话工厂】SQLAlchemy 的引擎和会话配置
#   定义位置：app/db/session.py
#   SessionLocal — sessionmaker 实例，用于创建数据库会话
#     - SQLite 模式：自动设置 WAL 模式、外键约束、busy_timeout
#     - PostgreSQL 模式：支持连接池配置（pool_size/max_overflow/timeout/recycle）
#   get_db() — FastAPI 依赖注入用的 DB 会话生成器
from app.db.session import SessionLocal

# 【LLM HTTP 客户端】线程安全的 httpx.Client 池，用于调用大模型 API
#   定义位置：app/llm/http_client.py
#   close_llm_http_client() — 应用关闭时释放所有 HTTP 连接
#     每个线程有独立的 httpx.Client（通过 threading.local 实现）
#     支持通过环境变量 LLM_HTTP_PROXY 配置代理
from app.llm.http_client import close_llm_http_client

# 【用户模型】users 表的 ORM 定义
#   定义位置：app/models/user.py
#   User 字段：id(主键)、email、password_hash、display_name、is_admin、created_at、updated_at
from app.models.user import User

# 【项目任务看门狗】后台线程，定期巡检异步任务的健康状态
#   定义位置：app/services/project_task_runtime_service.py
#   start_project_task_watchdog() — 启动一个守护线程，定期执行 reconcile：
#     1. 检测"运行中但心跳超时"的任务 → 标记为 failed
#     2. 检测"已入队但队列中丢失"的任务 → 重新入队
#     巡检间隔由 project_task_watchdog_interval_seconds 配置（默认 15 秒）
#   stop_project_task_watchdog() — 停止看门狗线程
from app.services.project_task_runtime_service import start_project_task_watchdog, stop_project_task_watchdog

# 【用户活跃度追踪】记录用户最后活跃时间
#   定义位置：app/services/user_activity_service.py
#   touch_user_activity() — 每次 API 请求时更新用户的 last_seen_at
#     内置内存级去重缓存，同一用户在 auth_activity_touch_interval_seconds 内（默认30秒）
#     不会重复写库，避免高频请求导致的数据库压力
#     数据写入 user_activity_stats 表
from app.services.user_activity_service import touch_user_activity

# ──────────────────────────────────────────────────────────────────
# 全局 logger 实例，名称为 "ainovel"
# 所有应用日志都通过这个 logger 输出，最终由 loguru 接管并格式化为 JSON
# ──────────────────────────────────────────────────────────────────
logger = logging.getLogger("ainovel")


# ═══════════════════════════════════════════════════════════════════
# 一、辅助函数 — 启动阶段使用的工具函数
# ═══════════════════════════════════════════════════════════════════


def _warn_sqlite_single_worker() -> None:
    """
    如果使用 SQLite 作为数据库，输出警告日志。

    SQLite 不支持并发写入，多 worker 模式下会出现 "database is locked" 错误，
    因此必须以 --workers 1 运行 uvicorn。

    判断逻辑：检查 settings.database_url 是否以 "sqlite" 开头
      实现位置：app/core/config.py → Settings.is_sqlite()
    """
    if not settings.is_sqlite():
        return
    log_event(
        logger,
        "warning",
        sqlite={
            "database_url": settings.database_url,
            "constraint": "run with --workers 1",
        },
        message="SQLite 模式仅支持单 worker；请使用 `uvicorn ... --workers 1`（避免 database is locked）",
    )


def _safe_error_details(details: object | None) -> dict | None:
    """
    过滤错误详情中的敏感信息，只保留白名单内的字段后用于日志输出。

    代理到 app/core/logging.py → safe_log_details()
    白名单字段包括：status_code、upstream_error、provider、model、latency_ms 等
    upstream_error 字段会额外经过密钥脱敏处理（正则替换 API Key 等）
    """
    return safe_log_details(details)


def _ensure_local_user() -> None:
    """
    在开发环境下，确保"本地回退用户"存在于数据库中。

    当 app_env == "dev" 且配置了 auth_dev_fallback_user_id（默认 "local-user"）时：
      1. 查询 users 表中是否存在该用户
      2. 不存在则创建，display_name 设为 "本地用户"

    这样开发时无需登录，中间件会自动将请求绑定到这个回退用户
    （见下方 auth_session_middleware 中的 dev_fallback 逻辑）

    涉及模型：app/models/user.py → User
    涉及会话：app/db/session.py → SessionLocal
    """
    if settings.app_env != "dev":
        return
    fallback_user_id = settings.auth_dev_fallback_user_id
    if not fallback_user_id:
        return
    db = SessionLocal()
    try:
        user = db.get(User, fallback_user_id)
        if user is None:
            db.add(User(id=fallback_user_id, display_name="本地用户"))
            db.commit()
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════
# 二、应用生命周期（Lifespan）— FastAPI 的启动与关闭钩子
# ═══════════════════════════════════════════════════════════════════


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """
    FastAPI 应用的生命周期管理器。

    【启动阶段】（yield 之前）— 应用启动时按顺序执行：
      1. configure_logging()
         → 初始化 loguru 日志系统，拦截所有 stdlib logger
         → 实现位置：app/core/logging.py

      2. bootstrap_in_app()
         → 由 app/bootstrap.py 的共享策略决定是否执行数据库迁移和管理员初始化

      3. _warn_sqlite_single_worker()
         → 如果用 SQLite，输出警告

      4. _ensure_local_user()
         → 开发环境下创建本地回退用户

      5. start_project_task_watchdog()
         → 启动后台看门狗守护线程，定期巡检异步任务健康状态
         → 实现位置：app/services/project_task_runtime_service.py
         → 巡检内容：超时任务标记失败 + 丢失任务重新入队

    【关闭阶段】（yield 之后，finally 块中）：
      1. stop_project_task_watchdog()
         → 停止看门狗线程

      2. close_llm_http_client()
         → 关闭所有 LLM HTTP 连接（httpx.Client 池）
         → 实现位置：app/llm/http_client.py
    """
    configure_logging()
    bootstrap_in_app()
    _warn_sqlite_single_worker()
    _ensure_local_user()
    watchdog_handle = start_project_task_watchdog()
    try:
        yield
    finally:
        stop_project_task_watchdog(watchdog_handle)
        close_llm_http_client()


# ═══════════════════════════════════════════════════════════════════
# 三、FastAPI 应用实例创建
# ═══════════════════════════════════════════════════════════════════

# 创建 FastAPI 应用实例
#   title="ainovel"           → API 文档（/docs）中显示的标题
#   version=settings.app_version → 版本号，来自配置（默认 "0.1.0"）
#   lifespan=lifespan         → 上面定义的生命周期管理器
app = FastAPI(title="ainovel", version=settings.app_version, lifespan=lifespan)


# ═══════════════════════════════════════════════════════════════════
# 四、中间件注册
# ═══════════════════════════════════════════════════════════════════
# 注意：FastAPI 中间件的执行顺序与注册顺序相反（洋葱模型）
# 即：最后注册的中间件最先执行
# 实际执行顺序：request_id_and_logging → auth_session → CORS → 路由处理

# ── 4.1 CORS 中间件 ──────────────────────────────────────────────
# 处理跨域请求，前端（默认 localhost:5173）需要跨域访问后端 API
#
# allow_origins：
#   - 优先使用 settings.cors_origins（配置文件/环境变量中的 CORS_ORIGINS）
#   - 如果未配置：prod 环境返回空列表（禁止跨域），dev 环境默认允许 localhost:5173
#
# allow_credentials=True：
#   - 允许跨域请求携带 Cookie（认证会话依赖 Cookie）
#
# allow_headers：
#   - Content-Type：标准请求头
#   - Authorization：Bearer Token 认证（预留）
#   - X-LLM-Provider：前端指定 LLM 提供商（如 openai/anthropic）
#   - X-LLM-API-Key：前端传递用户自己的 API Key
#
# expose_headers：
#   - X-Request-Id：让前端能读取到请求追踪 ID（用于排查问题）
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list() or ([] if settings.app_env == "prod" else ["http://localhost:5173"]),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["Content-Type", "Authorization", "X-LLM-Provider", "X-LLM-API-Key"],
    expose_headers=["X-Request-Id"],
)


# ── 4.2 认证会话中间件 ─────────────────────────────────────────────
@app.middleware("http")
async def auth_session_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """
    认证会话中间件 — 从 Cookie 中解析用户身份，挂载到 request.state 上。

    处理流程：
      1. 初始化 request.state 上的四个认证字段为 None
      2. 从 Cookie 中取出会话令牌（Cookie 名由 auth_cookie_user_id_name 配置，默认 "user_id"）
      3. 调用 decode_session_cookie() 验证签名和有效期
         → 实现位置：app/core/auth_session.py
      4. 如果验证成功：
         - request.state.user_id = 用户 ID
         - request.state.authenticated_user_id = 用户 ID（已认证标记）
         - request.state.session_expire_at = 会话过期时间
         - request.state.auth_source = "session"
      5. 如果验证失败且是开发环境：
         - 使用 auth_dev_fallback_user_id（默认 "local-user"）作为回退
         - request.state.auth_source = "dev_fallback"
         - 注意：此时 authenticated_user_id 仍为 None（未真正认证）

    后续路由中通过 request.state.user_id 获取当前用户 ID
    通过 request.state.authenticated_user_id 判断是否真正登录
    """
    request.state.user_id = None
    request.state.authenticated_user_id = None
    request.state.session_expire_at = None
    request.state.session_issued_at = None
    request.state.session_version = None
    request.state.auth_source = None

    cookie_value = request.cookies.get(settings.auth_cookie_user_id_name)
    session = decode_session_cookie(cookie_value) if cookie_value else None

    if session is not None:
        request.state.user_id = session.user_id
        request.state.authenticated_user_id = session.user_id
        request.state.session_expire_at = session.expires_at
        request.state.session_issued_at = session.issued_at
        request.state.session_version = session.session_version
        request.state.auth_source = "session"
    else:
        fallback_user_id = settings.auth_dev_fallback_user_id if settings.app_env == "dev" else None
        if fallback_user_id:
            request.state.user_id = fallback_user_id
            request.state.auth_source = "dev_fallback"

    return await call_next(request)


# ── 4.3 请求 ID 与日志中间件 ────────────────────────────────────────
@app.middleware("http")
async def request_id_and_logging_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """
    请求 ID 和访问日志中间件 — 每个请求分配唯一 ID，并记录访问日志。

    处理流程：
      1. 【请求 ID 生成】
         - 优先使用客户端传来的 X-Request-Id 头（支持链路追踪）
         - 否则生成 UUID v4 作为请求 ID
         - 存入 request.state.request_id 和 ContextVar（整个请求生命周期可见）
         → 实现位置：app/core/request_id.py

      2. 【执行请求并计时】
         - 记录开始时间 → 调用下游处理 → 计算耗时（毫秒）

      3. 【访问日志】
         - 如果响应状态码 < 400，输出 info 级别的结构化 JSON 日志
         - 包含：path、method、status_code、latency_ms
         → 实现位置：app/core/logging.py → log_event()

      4. 【用户活跃度更新】
         - 如果是已认证用户的 API 请求（排除 /api/health 和 OPTIONS）
         - 调用 touch_user_activity() 更新 user_activity_stats 表
         → 实现位置：app/services/user_activity_service.py
         - 内置去重缓存，默认 30 秒内同一用户不重复写库

      5. 【响应头】
         - 将 X-Request-Id 附加到响应头，前端可读取用于排查问题

      6. 【ContextVar 清理】
         - finally 块中重置 request_id 的 ContextVar，防止泄漏到其他请求
    """
    rid = request.headers.get("X-Request-Id") or new_request_id()
    request.state.request_id = rid
    token = set_request_id(rid)

    try:
        start = time.perf_counter()
        response = await call_next(request)

        latency_ms = int((time.perf_counter() - start) * 1000)
        if response.status_code < 400:
            log_event(
                logger,
                "info",
                path=request.url.path,
                method=request.method,
                status_code=response.status_code,
                latency_ms=latency_ms,
            )

        # 更新用户活跃度（仅对已认证的 API 请求，排除健康检查和预检请求）
        authenticated_user_id = getattr(request.state, "authenticated_user_id", None)
        if (
            isinstance(authenticated_user_id, str)
            and authenticated_user_id
            and request.url.path.startswith("/api/")
            and request.url.path != "/api/health"
            and request.method.upper() != "OPTIONS"
        ):
            try:
                touch_user_activity(
                    user_id=authenticated_user_id,
                    request_id=rid,
                    path=request.url.path,
                    method=request.method,
                    status_code=response.status_code,
                )
            except Exception as exc:
                log_event(
                    logger,
                    "warning",
                    event="USER_ACTIVITY",
                    action="touch_failed",
                    exception_type=type(exc).__name__,
                )

        response.headers["X-Request-Id"] = rid
        return response
    finally:
        reset_request_id(token)


# ═══════════════════════════════════════════════════════════════════
# 五、全局异常处理器
# ═══════════════════════════════════════════════════════════════════
# FastAPI 允许注册针对特定异常类型的全局处理器，
# 当路由处理函数抛出对应异常时，自动调用这些处理器生成统一格式的错误响应。
#
# 所有错误响应格式统一为：
# {
#   "ok": false,
#   "error": { "code": "ERROR_CODE", "message": "错误描述", "details": {...} },
#   "request_id": "uuid"
# }
# 响应头中始终包含 X-Request-Id 用于排查


# ── 5.1 框架 HTTP 异常处理器 ───────────────────────────────────────
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """将路由级 HTTP 异常转换为统一错误信封，并保留 Allow 等协议头。"""

    rid = getattr(request.state, "request_id", new_request_id())
    status_code = int(exc.status_code)
    code, message = {
        404: ("NOT_FOUND", "Not Found"),
        405: ("METHOD_NOT_ALLOWED", "Method Not Allowed"),
    }.get(status_code, (f"HTTP_{status_code}", "HTTP Error"))
    log_event(
        logger,
        "warning" if status_code < 500 else "error",
        path=request.url.path,
        method=request.method,
        status_code=status_code,
        error_code=code,
        message=message,
    )
    headers = {key: value for key, value in (exc.headers or {}).items() if key.lower() != "x-request-id"}
    headers["X-Request-Id"] = rid
    payload = error_payload(request_id=rid, code=code, message=message, details={})
    return JSONResponse(payload, status_code=status_code, headers=headers)


# ── 5.2 业务异常处理器 ─────────────────────────────────────────────
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """
    处理所有 AppError 业务异常。

    AppError 是项目自定义的业务异常基类（定义在 app/core/errors.py），
    各路由和服务层通过抛出 AppError 来表达业务错误。

    常见场景及对应的 AppError 子类型：
      - 未登录         → AppError.unauthorized()  → 401
      - 无权限         → AppError.forbidden()     → 403
      - 资源不存在     → AppError.not_found()     → 404
      - 资源冲突       → AppError.conflict()      → 409
      - 参数错误       → AppError.validation()    → 400

    日志级别：
      - status_code < 500 → warning（客户端错误，正常业务流程）
      - status_code >= 500 → error（不应出现的服务端错误）
    """
    rid = getattr(request.state, "request_id", new_request_id())
    log_event(
        logger,
        "warning" if exc.status_code < 500 else "error",
        path=request.url.path,
        method=request.method,
        status_code=exc.status_code,
        error_code=exc.code,
        message=exc.message,
        details=_safe_error_details(exc.details),
    )
    payload = error_payload(request_id=rid, code=exc.code, message=exc.message, details=exc.details)
    headers = safe_app_error_headers(exc.headers)
    headers["X-Request-Id"] = rid
    response = JSONResponse(payload, status_code=exc.status_code, headers=headers)
    if exc.code == "ACCOUNT_DISABLED":
        clear_session_cookies(response)
    return response


# ── 5.3 请求参数校验异常处理器 ──────────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """
    处理 FastAPI/Pydantic 的请求参数校验错误。

    当请求的 path/query/body 参数不符合路由定义的 Pydantic 模型时触发。
    例如：缺少必填字段、类型不匹配、值不在枚举范围内等。

    安全处理：只保留错误中的 loc（位置）、msg（消息）、type（类型）三个字段，
    过滤掉可能包含敏感数据的 input/ctx 等字段。

    始终返回 400 状态码，错误码为 "VALIDATION_ERROR"。
    """
    rid = getattr(request.state, "request_id", new_request_id())
    safe_errors = [
        {k: v for k, v in e.items() if k in ("loc", "msg", "type")} for e in exc.errors() if isinstance(e, dict)
    ]
    log_event(
        logger,
        "warning",
        path=request.url.path,
        method=request.method,
        status_code=400,
        error_code="VALIDATION_ERROR",
        message="参数校验失败",
        details={"errors": safe_errors},
    )
    payload = error_payload(
        request_id=rid,
        code="VALIDATION_ERROR",
        message="参数校验失败",
        details={"errors": safe_errors},
    )
    return JSONResponse(payload, status_code=400, headers={"X-Request-Id": rid})


# ── 5.4 数据库异常处理器 ───────────────────────────────────────────
@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    """
    处理所有 SQLAlchemy 数据库层面的异常。

    典型场景：连接超时、约束冲突、SQL 执行错误、事务死锁等。

    日志处理：
      - dev 环境：记录完整异常类型 + 脱敏后的异常消息 + 堆栈（前 500 字符）
      - prod 环境：只记录异常类型 + 异常消息的 SHA256 前 12 位 hash（保护敏感信息）
      → 实现位置：app/core/logging.py → exception_log_fields()

    始终返回 500 状态码，错误码为 "DB_ERROR"，消息为 "数据库错误"。
    不向前端暴露任何数据库内部错误细节。
    """
    rid = getattr(request.state, "request_id", new_request_id())
    log_event(
        logger,
        "error",
        path=request.url.path,
        method=request.method,
        status_code=500,
        error="DB_ERROR",
        **exception_log_fields(exc),
    )
    payload = error_payload(request_id=rid, code="DB_ERROR", message="数据库错误", details={})
    return JSONResponse(payload, status_code=500, headers={"X-Request-Id": rid})


# ── 5.5 兜底异常处理器 ────────────────────────────────────────────
@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    处理所有未被上面处理器捕获的异常（兜底处理器）。

    这是最后一道防线，确保任何未预料的异常都不会导致原始错误信息泄漏给前端。

    始终返回 500 状态码，错误码为 "INTERNAL_ERROR"，消息为 "服务器内部错误"。
    详细的异常信息只记录在服务端日志中。
    """
    rid = getattr(request.state, "request_id", new_request_id())
    log_event(
        logger,
        "error",
        path=request.url.path,
        method=request.method,
        status_code=500,
        error="UNHANDLED_EXCEPTION",
        **exception_log_fields(exc),
    )
    payload = error_payload(request_id=rid, code="INTERNAL_ERROR", message="服务器内部错误", details={})
    return JSONResponse(payload, status_code=500, headers={"X-Request-Id": rid})


# ═══════════════════════════════════════════════════════════════════
# 六、路由挂载 — 将所有 API 路由注册到应用
# ═══════════════════════════════════════════════════════════════════
# api_router 定义在 app/api/router.py，prefix="/api"
# 它聚合了以下 28 个路由模块（每个模块在 app/api/routes/ 目录下有对应文件）：
#
# 【基础设施类】
#   health          → GET  /api/health              — 健康检查（k8s/监控探针）
#   auth            → POST /api/auth/...            — 登录/注册/登出/会话刷新/OIDC回调
#   settings        → GET|PUT /api/settings/...     — 系统级设置的读写
#
# 【项目管理类】
#   projects        → CRUD /api/projects/...        — 小说项目的增删改查
#   export          → GET  /api/export/...          — 项目导出（JSON/文本等格式）
#   import_export   → POST /api/import-export/...   — 项目导入
#
# 【小说内容类】
#   chapters        → CRUD /api/chapters/...        — 章节管理
#   characters      → CRUD /api/characters/...      — 角色管理
#   entries         → CRUD /api/entries/...          — 条目管理（世界观/设定等）
#
# 【大纲体系】
#   outline         → /api/outline/...              — 大纲编辑器交互
#   outlines        → /api/outlines/...             — 大纲列表（一个项目可有多个大纲）
#   detailed_outlines → /api/detailed-outlines/...  — 细纲（大纲的详细展开）
#   outline_parse   → /api/outline-parse/...        — 大纲文本解析（AI 辅助解析结构）
#
# 【AI 生成类】
#   batch_generation → /api/batch-generation/...    — 批量 AI 生成任务（创建/状态/取消）
#   generation_runs  → /api/generation-runs/...     — 生成运行记录查询
#
# 【LLM 配置类】
#   llm             → /api/llm/...                  — LLM 调用入口（chat/生成/测试连接）
#   llm_models      → /api/llm-models/...           — 可用模型列表
#   llm_profiles    → /api/llm-profiles/...         — LLM 配置档案（不同场景用不同配置）
#   llm_preset      → /api/llm-preset/...           — LLM 参数预设（temperature/top_p 等）
#   llm_task_presets → /api/llm-task-presets/...     — 按任务类型的 LLM 预设
#   llm_capabilities → /api/llm-capabilities/...    — LLM 能力查询
#
# 【提示词工程】
#   prompts         → /api/prompts/...              — 提示词模板 CRUD
#   prompt_studio   → /api/prompt-studio/...        — 提示词工作室（交互式调试提示词）
#
# 【记忆与搜索】
#   memory          → /api/memory/...               — 记忆管理（项目上下文记忆）
#   story_memory    → /api/story-memory/...         — 故事记忆（角色/情节/世界观记忆）
#   search          → /api/search/...               — 全文搜索
#   vector          → /api/vector/...               — 向量检索（语义搜索，支持 Chroma/pgvector）
#
# 【其他】
#   mcp             → /api/mcp/...                  — MCP 协议端点
#   writing_styles  → /api/writing-styles/...       — 写作风格管理
app.include_router(api_router)
