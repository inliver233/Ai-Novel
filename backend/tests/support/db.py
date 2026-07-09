"""内存 SQLite 引擎与会话工厂。

替换旧测试里 ~62 份复制的 ``sqlite:///:memory:`` + ``StaticPool`` 样板。

为什么用 ``StaticPool`` + ``check_same_thread=False``：Starlette ``TestClient``
在独立 worker 线程里跑同步 app，引擎必须跨线程可用——这是全仓既有测试的统一
约定（见 ``tests/test_auth_session.py:76-80``）。
"""

from __future__ import annotations

from typing import Iterable

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base


def make_sqlite_engine() -> Engine:
    """创建一个跨线程可用的内存 SQLite 引擎。

    调用方负责在用完后 ``engine.dispose()``；在 unittest 风格里通常用
    ``self.addCleanup(engine.dispose)``，在 pytest 风格里用 fixture 的 finalizer。
    """
    return create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def make_session_factory(
    engine: Engine,
    *,
    autoflush: bool = False,
    expire_on_commit: bool = False,
) -> sessionmaker:
    """创建绑定到指定引擎的 ``sessionmaker``。

    ``autoflush=False`` + ``expire_on_commit=False`` 与全仓既有测试的会话工厂一致，
    保持默认值相同，迁移过来的测试行为不变。
    """
    return sessionmaker(bind=engine, autoflush=autoflush, expire_on_commit=expire_on_commit)


def create_tables(engine: Engine, models: Iterable[type[DeclarativeBase]] | None = None) -> None:
    """在引擎上建表。

    - ``models=None``：建所有已注册进 ``Base.metadata`` 的活表（lite 裁剪后的死模型
      未注册进 metadata，故不会被建——与生产一致）。
    - ``models=[ModelA, ModelB]``：选择性建表。当一个测试只需要少数表时优先用此形式，
      可避免触发 pgvector/chromadb 专属表的 DDL（那些表在生产里由 raw SQL 迁移创建，
      不在 metadata 中，本就不会被建，但显式列出所需表让测试意图更清晰、更快）。
    """
    if models is None:
        Base.metadata.create_all(engine)
        return
    tables = [m.__table__ for m in models]
    Base.metadata.create_all(engine, tables=tables)


def session_scope(factory: sessionmaker) -> Session:
    """从工厂创建一个会话，供需要直接操作 DB 的测试使用。

    调用方负责 ``db.close()``。配合 ``with`` 上下文最简：

        with factory() as db:
            ...

    （``sessionmaker`` 实例本身就是可调用且支持上下文管理器协议。）
    """
    return factory()
