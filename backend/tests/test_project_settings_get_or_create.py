"""backend-data#2 残留：ProjectSettings 惰性建行必须收敛到单一入口。

get_or_create_project_settings 是唯一允许构造 ProjectSettings 的生产代码位置：
竞态安全（on_conflict_do_nothing）、列默认完整、不代管事务（caller 提交）。
AST 护栏防止散点 `ProjectSettings(...)` 回潮。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.project_settings import ProjectSettings
from app.services import project_settings_service
from app.services.project_settings_service import get_or_create_project_settings

_APP_ROOT = Path(__file__).resolve().parents[1] / "app"
_ALLOWED_CONSTRUCTOR_FILES = {_APP_ROOT / "services" / "project_settings_service.py"}


def test_project_settings_constructor_is_confined_to_single_entry() -> None:
    offenders: list[str] = []
    for path in _APP_ROOT.rglob("*.py"):
        if path in _ALLOWED_CONSTRUCTOR_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name == "ProjectSettings":
                offenders.append(f"{path.relative_to(_APP_ROOT)}:{node.lineno}")
    assert offenders == [], f"ProjectSettings 惰性建行必须走 get_or_create_project_settings: {offenders}"


def _factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[ProjectSettings.__table__])
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def test_creates_row_with_column_defaults() -> None:
    factory = _factory()
    with factory() as db:
        row = get_or_create_project_settings(db, project_id="p1")
        db.commit()
        assert row.project_id == "p1"
        assert row.world_setting is None
        assert row.auto_update_characters_enabled is True
        assert row.vector_index_dirty is False
        assert int(row.vector_dirty_revision or 0) == 0
        assert int(row.vector_embedding_expected_dimension) == 1536


def test_returns_existing_row_unchanged() -> None:
    factory = _factory()
    with factory() as db:
        first = get_or_create_project_settings(db, project_id="p1")
        first.world_setting = "已有世界观"
        db.commit()
    with factory() as db:
        row = get_or_create_project_settings(db, project_id="p1")
        assert row.world_setting == "已有世界观"


class _FirstGetNoneProxy:
    """模拟并发首建竞态：第一次 db.get 看不到行，但 INSERT 冲突。"""

    def __init__(self, real: Session) -> None:
        self._real = real
        self._first_get = True

    def get(self, *args: Any, **kwargs: Any) -> Any:
        if self._first_get:
            self._first_get = False
            return None
        return self._real.get(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_lost_create_race_reads_back_row_and_logs_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = _factory()
    with factory() as seed:
        get_or_create_project_settings(seed, project_id="p1")
        seed.commit()

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        project_settings_service,
        "log_event",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    with factory() as db:
        row = get_or_create_project_settings(_FirstGetNoneProxy(db), project_id="p1")  # type: ignore[arg-type]
        assert row.project_id == "p1"

    assert len(calls) == 1
    args, fields = calls[0]
    assert args[1] == "warning"
    assert fields["event"] == "PROJECT_SETTINGS"
    assert fields["action"] == "create_race_lost"
    assert fields["project_id"] == "p1"
