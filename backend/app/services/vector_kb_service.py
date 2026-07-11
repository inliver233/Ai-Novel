from __future__ import annotations

import re
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.utils import new_id
from app.models.knowledge_base import KnowledgeBase


_DEFAULT_KB_ID = "default"
_KB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_KB_PRIORITY_GROUPS = {"normal", "high"}


def ensure_default_kb(db: Session, *, project_id: str, commit: bool = True) -> KnowledgeBase:
    row = (
        db.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.project_id == project_id,
                KnowledgeBase.kb_id == _DEFAULT_KB_ID,
            )
        )
        .scalars()
        .first()
    )
    if row is not None:
        return row

    row = KnowledgeBase(
        id=new_id(),
        project_id=project_id,
        kb_id=_DEFAULT_KB_ID,
        name="Default",
        enabled=True,
        weight=1.0,
        order_index=0,
        priority_group="normal",
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def list_kbs(db: Session, *, project_id: str) -> list[KnowledgeBase]:
    ensure_default_kb(db, project_id=project_id)
    return (
        db.execute(
            select(KnowledgeBase)
            .where(KnowledgeBase.project_id == project_id)
            .order_by(KnowledgeBase.order_index.asc(), KnowledgeBase.kb_id.asc())
        )
        .scalars()
        .all()
    )


def get_kb(db: Session, *, project_id: str, kb_id: str) -> KnowledgeBase:
    row = (
        db.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.project_id == project_id,
                KnowledgeBase.kb_id == kb_id,
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        raise AppError.not_found("KB 不存在")
    return row


def create_kb(
    db: Session,
    *,
    project_id: str,
    name: str,
    kb_id: str | None = None,
    enabled: bool = True,
    weight: float = 1.0,
    priority_group: str | None = None,
) -> KnowledgeBase:
    ensure_default_kb(db, project_id=project_id)

    n = str(name or "").strip()
    if not n:
        raise AppError.validation("name 不能为空")

    requested = str(kb_id or "").strip()
    if requested:
        if requested == _DEFAULT_KB_ID:
            raise AppError.validation("kb_id 不可使用 default（保留）")
        if not _KB_ID_RE.match(requested):
            raise AppError.validation("kb_id 仅允许字母数字_- 且长度<=64")
        new_kb_id = requested
    else:
        new_kb_id = ""
        for _ in range(20):
            candidate = f"kb_{uuid4().hex[:10]}"
            exists = (
                db.execute(
                    select(KnowledgeBase.id).where(
                        KnowledgeBase.project_id == project_id,
                        KnowledgeBase.kb_id == candidate,
                    )
                )
                .scalars()
                .first()
                is not None
            )
            if not exists:
                new_kb_id = candidate
                break
        if not new_kb_id:
            new_kb_id = f"kb_{uuid4().hex}"
            new_kb_id = new_kb_id[:64]

    max_order = db.execute(
        select(func.max(KnowledgeBase.order_index)).where(KnowledgeBase.project_id == project_id)
    ).scalar()
    next_order = int(max_order or 0) + 1
    priority = str(priority_group or "").strip().lower() or "normal"
    if priority not in _KB_PRIORITY_GROUPS:
        raise AppError.validation("priority_group 仅允许 normal|high")

    row = KnowledgeBase(
        id=new_id(),
        project_id=project_id,
        kb_id=new_kb_id,
        name=n,
        enabled=bool(enabled),
        weight=float(weight),
        order_index=next_order,
        priority_group=priority,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError.conflict("kb_id 已存在") from None
    db.refresh(row)
    return row


def update_kb(
    db: Session,
    *,
    project_id: str,
    kb_id: str,
    name: str | None = None,
    enabled: bool | None = None,
    weight: float | None = None,
    priority_group: str | None = None,
) -> KnowledgeBase:
    ensure_default_kb(db, project_id=project_id)
    row = get_kb(db, project_id=project_id, kb_id=kb_id)

    if name is not None:
        n = str(name or "").strip()
        if not n:
            raise AppError.validation("name 不能为空")
        row.name = n
    if enabled is not None:
        row.enabled = bool(enabled)
    if weight is not None:
        row.weight = float(weight)
    if priority_group is not None:
        priority = str(priority_group or "").strip().lower() or "normal"
        if priority not in _KB_PRIORITY_GROUPS:
            raise AppError.validation("priority_group 仅允许 normal|high")
        row.priority_group = priority

    db.commit()
    db.refresh(row)
    return row


def delete_kb(db: Session, *, project_id: str, kb_id: str) -> None:
    ensure_default_kb(db, project_id=project_id)
    if kb_id == _DEFAULT_KB_ID:
        raise AppError.validation("default KB 不可删除")
    row = get_kb(db, project_id=project_id, kb_id=kb_id)
    if bool(row.enabled):
        raise AppError.validation("请先禁用 KB 再删除")
    db.delete(row)
    db.commit()


def reorder_kbs(db: Session, *, project_id: str, ordered_kb_ids: list[str]) -> list[KnowledgeBase]:
    ensure_default_kb(db, project_id=project_id)
    rows = list_kbs(db, project_id=project_id)

    desired = [str(x or "").strip() for x in (ordered_kb_ids or []) if str(x or "").strip()]
    desired_unique: list[str] = []
    seen: set[str] = set()
    for kb_id in desired:
        if kb_id in seen:
            continue
        seen.add(kb_id)
        desired_unique.append(kb_id)

    existing_ids = [r.kb_id for r in rows]
    if set(desired_unique) != set(existing_ids):
        raise AppError.validation("kb_ids 必须包含所有 KB（且不可包含未知项）")

    by_id = {r.kb_id: r for r in rows}
    for idx, kb_id in enumerate(desired_unique):
        by_id[kb_id].order_index = idx

    db.commit()
    return list_kbs(db, project_id=project_id)


def resolve_query_kbs(db: Session, *, project_id: str, requested_kb_ids: list[str] | None) -> list[KnowledgeBase]:
    rows = list_kbs(db, project_id=project_id)
    by_id = {r.kb_id: r for r in rows}

    requested = [str(x or "").strip() for x in (requested_kb_ids or []) if str(x or "").strip()]
    requested_unique: list[str] = []
    seen: set[str] = set()
    for kb_id in requested:
        if kb_id in seen:
            continue
        seen.add(kb_id)
        requested_unique.append(kb_id)

    if requested_unique:
        missing = [kb_id for kb_id in requested_unique if kb_id not in by_id]
        if missing:
            raise AppError.not_found("KB 不存在", details={"missing_kb_ids": missing})
        return [by_id[kb_id] for kb_id in requested_unique]

    enabled = [r for r in rows if bool(r.enabled)]
    if enabled:
        return enabled

    default_kb = by_id.get(_DEFAULT_KB_ID)
    if default_kb is None:
        default_kb = ensure_default_kb(db, project_id=project_id)
    return [default_kb]
