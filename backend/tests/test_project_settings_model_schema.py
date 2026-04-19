from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_settings import ProjectSettings
from app.models.user import User


class TestProjectSettingsModelSchema(unittest.TestCase):
    def test_auto_update_columns_match_runtime_schema_expectations(self) -> None:
        columns = set(ProjectSettings.__table__.columns.keys())
        expected = {
            "auto_update_worldbook_enabled",
            "auto_update_characters_enabled",
            "auto_update_story_memory_enabled",
            "auto_update_graph_enabled",
            "auto_update_vector_enabled",
            "auto_update_search_enabled",
            "auto_update_fractal_enabled",
            "auto_update_tables_enabled",
        }
        self.assertTrue(expected.issubset(columns), expected - columns)

    def test_new_project_settings_rows_persist_all_auto_update_defaults(self) -> None:
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.addCleanup(engine.dispose)

        Base.metadata.create_all(
            engine,
            tables=[
                User.__table__,
                Project.__table__,
                Outline.__table__,
                ProjectSettings.__table__,
            ],
        )
        SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        with SessionLocal() as db:
            db.add(User(id="u1", display_name="User 1"))
            db.add(Project(id="p1", owner_user_id="u1", name="Project 1", genre=None, logline=None))
            db.add(ProjectSettings(project_id="p1"))
            db.commit()

            row = db.get(ProjectSettings, "p1")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertTrue(row.auto_update_worldbook_enabled)
            self.assertTrue(row.auto_update_characters_enabled)
            self.assertTrue(row.auto_update_story_memory_enabled)
            self.assertTrue(row.auto_update_graph_enabled)
            self.assertTrue(row.auto_update_vector_enabled)
            self.assertTrue(row.auto_update_search_enabled)
            self.assertTrue(row.auto_update_fractal_enabled)
            self.assertTrue(row.auto_update_tables_enabled)
            self.assertFalse(row.vector_index_dirty)


if __name__ == "__main__":
    unittest.main()
