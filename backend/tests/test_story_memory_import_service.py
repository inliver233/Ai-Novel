from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.errors import AppError
from app.db.base import Base
from app.models.chapter import Chapter
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_settings import ProjectSettings
from app.models.story_memory import StoryMemory
from app.models.user import User
from app.schemas.story_memory_import import StoryMemoryImportV1Item
from app.services.story_memory_import_service import import_story_memories


class TestStoryMemoryImportService(unittest.TestCase):
    """Exercise the public story-memory import application service."""

    def setUp(self) -> None:
        engine = create_engine(
            'sqlite:///:memory:',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(
            engine,
            tables=[
                User.__table__,
                Project.__table__,
                Outline.__table__,
                Chapter.__table__,
                ProjectSettings.__table__,
                StoryMemory.__table__,
            ],
        )
        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        with self.SessionLocal() as db:
            db.add(User(id='u_owner', display_name='owner'))
            db.add(Project(id='p1', owner_user_id='u_owner', name='Project 1', genre=None, logline=None))
            db.add(Outline(id='o1', project_id='p1', title='Outline', content_md=None, structure_json=None))
            db.add(Chapter(id='c1', project_id='p1', outline_id='o1', number=1, title='Ch1', status='done'))
            db.commit()

    def test_import_story_memories_payload_creates_rows_and_marks_rebuild_dirty(self) -> None:
        with self.SessionLocal() as db, patch(
            'app.services.story_memory_import_service.schedule_vector_rebuild_task',
            return_value='vector-task',
        ) as mock_vector, patch(
            'app.services.story_memory_import_service.schedule_search_rebuild_task',
            return_value='search-task',
        ) as mock_search:
            payload = import_story_memories(
                db,
                project_id='p1',
                schema_version='story_memory_import_v1',
                items=[
                    StoryMemoryImportV1Item(
                        memory_type=' fact ',
                        title=' Imported ',
                        content=' imported payload ',
                        importance_score=0.2,
                        story_timeline=40,
                        is_foreshadow=0,
                    )
                ],
                actor_user_id='u_owner',
                request_id='rid-import',
            )
            self.assertEqual(payload['created'], 1)
            self.assertEqual(len(payload['ids']), 1)
            # 导入应触发向量 / 搜索重建调度。
            mock_vector.assert_called_once()
            mock_search.assert_called_once()

        with self.SessionLocal() as db:
            settings = db.get(ProjectSettings, 'p1')
            self.assertIsNotNone(settings)
            assert settings is not None
            self.assertTrue(settings.vector_index_dirty)
            row = db.query(StoryMemory).one()
            self.assertEqual(row.memory_type, 'fact')
            self.assertEqual(row.title, 'Imported')
            self.assertEqual(row.content, 'imported payload')
            self.assertEqual(row.importance_score, 0.2)
            self.assertEqual(row.story_timeline, 40)
            self.assertEqual(row.metadata_json, '{"source": "import_all"}')

    def test_import_story_memories_rejects_unknown_or_missing_schema(self) -> None:
        for schema_version in ('story_memory_import_v2', None):
            with self.subTest(schema_version=schema_version), self.SessionLocal() as db:
                with self.assertRaises(AppError):
                    import_story_memories(
                        db,
                        project_id='p1',
                        schema_version=schema_version,
                        items=[],
                        actor_user_id='u_owner',
                        request_id='rid-import',
                    )

    def test_import_story_memories_skips_blank_content(self) -> None:
        with self.SessionLocal() as db:
            with self.assertRaises(AppError):
                import_story_memories(
                    db,
                    project_id='p1',
                    schema_version='story_memory_import_v1',
                    items=[
                        StoryMemoryImportV1Item(
                            memory_type='fact',
                            content='   ',
                            title=None,
                            importance_score=0.0,
                            story_timeline=0,
                            is_foreshadow=0,
                        )
                    ],
                    actor_user_id='u_owner',
                    request_id='rid-import',
                )
            self.assertEqual(db.query(StoryMemory).count(), 0)

    def test_import_story_memories_payload_rejects_empty_items(self) -> None:
        with self.SessionLocal() as db:
            with self.assertRaises(AppError):
                import_story_memories(
                    db,
                    project_id='p1',
                    schema_version='story_memory_import_v1',
                    items=[],
                    actor_user_id='u_owner',
                    request_id='rid-import',
                )


if __name__ == '__main__':
    unittest.main()
