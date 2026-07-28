from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.project_settings import ProjectSettings
from app.services.project_settings_service import get_or_create_project_settings


def mark_vector_index_dirty(db: Session, *, project_id: str) -> int:
    row = get_or_create_project_settings(db, project_id=project_id)
    db.execute(
        update(ProjectSettings)
        .where(ProjectSettings.project_id == project_id)
        .values(
            vector_index_dirty=True,
            vector_dirty_revision=ProjectSettings.vector_dirty_revision + 1,
        )
    )
    db.flush()
    db.refresh(row)
    return int(row.vector_dirty_revision)
