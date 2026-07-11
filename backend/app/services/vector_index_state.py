from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.project_settings import ProjectSettings


def mark_vector_index_dirty(db: Session, *, project_id: str) -> int:
    row = db.get(ProjectSettings, project_id)
    if row is None:
        row = ProjectSettings(project_id=project_id)
        db.add(row)
        db.flush()
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
