from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError
from app.models.batch_generation_task import BatchGenerationQuotaGuard, BatchGenerationTask
from app.services.batch_generation_commands import ACTIVE_BATCH_GENERATION_STATUSES



def enter_batch_generation_quota_admission(db: Session) -> None:
    """Start the quota admission transaction before any pending mutation.

    SQLite must acquire its database write reservation before the guard upsert.
    Ending the preceding read-only ORM transaction avoids attempting to upgrade a
    stale read snapshot, which otherwise produces ``database is locked`` under
    concurrent admission. Callers must re-read every mutable row afterwards.
    """

    if db.new or db.dirty or db.deleted:
        raise RuntimeError("batch quota admission must begin before pending ORM mutations")
    if db.get_bind().dialect.name == "sqlite":
        db.rollback()
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    else:
        db.expire_all()


@dataclass(frozen=True, slots=True)
class _QuotaScope:
    scope_type: str
    scope_key: str
    column: object
    limit: int
    message: str


def _normalized_scope_key(value: str, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 64:
        raise AppError.validation(message=f"{field} 不合法", details={"field": field})
    return normalized


def _quota_scopes(*, project_id: str, user_id: str, provider: str) -> tuple[_QuotaScope, ...]:
    project_key = _normalized_scope_key(project_id, field="project_id")
    user_key = _normalized_scope_key(user_id, field="user_id")
    provider_key = _normalized_scope_key(provider, field="runtime_provider").lower()
    return tuple(
        sorted(
            (
                _QuotaScope(
                    scope_type="project",
                    scope_key=project_key,
                    column=BatchGenerationTask.project_id,
                    limit=int(settings.batch_generation_project_active_limit),
                    message="当前项目已有过多进行中的批量生成任务",
                ),
                _QuotaScope(
                    scope_type="provider",
                    scope_key=provider_key,
                    column=BatchGenerationTask.runtime_provider,
                    limit=int(settings.batch_generation_provider_active_limit),
                    message="当前模型提供方已有过多进行中的批量生成任务",
                ),
                _QuotaScope(
                    scope_type="user",
                    scope_key=user_key,
                    column=BatchGenerationTask.actor_user_id,
                    limit=int(settings.batch_generation_user_active_limit),
                    message="当前用户已有过多进行中的批量生成任务",
                ),
            ),
            key=lambda scope: (scope.scope_type, scope.scope_key),
        )
    )


def _upsert_quota_guards(db: Session, *, scopes: tuple[_QuotaScope, ...]) -> None:
    rows = [{"scope_type": scope.scope_type, "scope_key": scope.scope_key} for scope in scopes]
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(BatchGenerationQuotaGuard).values(rows).on_conflict_do_nothing(
            index_elements=["scope_type", "scope_key"]
        )
    elif dialect == "sqlite":
        statement = sqlite_insert(BatchGenerationQuotaGuard).values(rows).on_conflict_do_nothing(
            index_elements=["scope_type", "scope_key"]
        )
    else:  # The application supports SQLite locally and PostgreSQL in production.
        raise RuntimeError(f"batch generation quota locking does not support database dialect {dialect!r}")
    db.execute(statement)


def lock_and_enforce_batch_generation_quotas(
    db: Session,
    *,
    project_id: str,
    user_id: str,
    provider: str,
    ignore_task_id: str | None = None,
) -> None:
    """Serialize quota admission and count active memberships inside the lock."""

    scopes = _quota_scopes(project_id=project_id, user_id=user_id, provider=provider)
    _upsert_quota_guards(db, scopes=scopes)

    guard_keys = [(scope.scope_type, scope.scope_key) for scope in scopes]
    locked = db.execute(
        select(BatchGenerationQuotaGuard.scope_type, BatchGenerationQuotaGuard.scope_key)
        .where(tuple_(BatchGenerationQuotaGuard.scope_type, BatchGenerationQuotaGuard.scope_key).in_(guard_keys))
        .order_by(BatchGenerationQuotaGuard.scope_type.asc(), BatchGenerationQuotaGuard.scope_key.asc())
        .with_for_update()
    ).all()
    if len(locked) != len(scopes):  # pragma: no cover - defensive database invariant
        raise RuntimeError("failed to acquire every batch generation quota guard")

    ignored_id = str(ignore_task_id or "").strip() or None
    for scope in scopes:
        conditions = [
            BatchGenerationTask.status.in_(ACTIVE_BATCH_GENERATION_STATUSES),
            scope.column == scope.scope_key,
        ]
        if ignored_id is not None:
            conditions.append(BatchGenerationTask.id != ignored_id)
        active_count = int(
            db.execute(select(func.count()).select_from(BatchGenerationTask).where(*conditions)).scalar_one()
        )
        if active_count >= scope.limit:
            raise AppError(
                code="BATCH_GENERATION_QUOTA_EXCEEDED",
                message=scope.message,
                status_code=409,
                details={
                    "quota": scope.scope_type,
                    "scope_key": scope.scope_key,
                    "active_count": active_count,
                    "limit": scope.limit,
                },
            )
