"""Single entry point for lazily materializing a project's settings row.

backend-data#2: scattered ``ProjectSettings(project_id=...)`` construction is
forbidden — this helper is the only place allowed to create the row. It is
race-safe (``ON CONFLICT DO NOTHING``), relies on column server defaults, and
never commits: the caller owns the transaction.
"""

from __future__ import annotations

import logging

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.models.project_settings import ProjectSettings

logger = logging.getLogger("ainovel")


def get_or_create_project_settings(db: Session, *, project_id: str) -> ProjectSettings:
    row = db.get(ProjectSettings, project_id)
    if row is not None:
        return row

    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        statement = (
            postgresql_insert(ProjectSettings)
            .values(project_id=project_id)
            .on_conflict_do_nothing(index_elements=["project_id"])
        )
    elif dialect == "sqlite":
        statement = (
            sqlite_insert(ProjectSettings)
            .values(project_id=project_id)
            .on_conflict_do_nothing(index_elements=["project_id"])
        )
    else:  # The application supports SQLite locally and PostgreSQL in production.
        raise RuntimeError(f"project settings creation does not support database dialect {dialect!r}")

    result = db.execute(statement)
    if not getattr(result, "rowcount", 0):
        # 并发首建竞争失败：另一事务已插入，读回既有行即可（留痕观测竞争频率）。
        log_event(
            logger,
            "warning",
            event="PROJECT_SETTINGS",
            action="create_race_lost",
            project_id=project_id,
        )

    row = db.get(ProjectSettings, project_id)
    if row is None:
        raise RuntimeError(f"project settings row missing after upsert for project {project_id!r}")
    return row
