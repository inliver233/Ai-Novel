from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.memory_route_models import StoryMemoryImportV1Item
from app.api.routes.memory_route_story_helpers import (
    _ensure_story_memory_rebuild_dirty,
    _import_story_memories_payload,
    _validate_story_memory_import_schema_version,
)
from app.api.routes.memory_route_story_mappers import _build_story_memory_import_row
from app.core.errors import AppError
from app.db.base import Base
from app.models.chapter import Chapter
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_settings import ProjectSettings
from app.models.story_memory import StoryMemory
from app.models.user import User

UTC = timezone.utc


class TestMemoryRouteStoryHelpers(unittest.TestCase):
    """story_memory 导入路径与重建脏标记的活功能测试。

    注：历史版本还覆盖了 "open_loops 列表" / "foreshadow 解析" 等子功能，
    这些 helper 已随 lite 裁剪删除（story_memory 路由仅保留 CRUD + merge + import），
    故此处只测当前仍存在的导入与脏标记路径。
    """

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

    def test_build_story_memory_import_row_trims_and_skips_blank_content(self) -> None:
        now = datetime(2026, 3, 17, 8, 0, tzinfo=UTC)
        row = _build_story_memory_import_row(
            project_id='p1',
            item=StoryMemoryImportV1Item(
                memory_type=' fact ',
                title='  Imported  ',
                content='  imported content  ',
                importance_score=0.4,
                story_timeline=7,
                is_foreshadow=1,
            ),
            now=now,
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.memory_type, 'fact')
        self.assertEqual(row.title, 'Imported')
        self.assertEqual(row.content, 'imported content')
        self.assertEqual(row.metadata_json, '{"source": "import_all"}')

        blank = _build_story_memory_import_row(
            project_id='p1',
            item=StoryMemoryImportV1Item(memory_type='fact', title=None, content='   ', importance_score=0.0, story_timeline=0, is_foreshadow=0),
            now=now,
        )
        self.assertIsNone(blank)

    def test_validate_story_memory_import_schema_version_rejects_unknown(self) -> None:
        _validate_story_memory_import_schema_version('story_memory_import_v1')
        with self.assertRaises(AppError):
            _validate_story_memory_import_schema_version('story_memory_import_v2')
        with self.assertRaises(AppError):
            _validate_story_memory_import_schema_version(None)

    def test_import_story_memories_payload_creates_rows_and_marks_rebuild_dirty(self) -> None:
        with self.SessionLocal() as db, patch(
            'app.api.routes.memory_route_story_helpers.schedule_vector_rebuild_task',
            return_value='vector-task',
        ) as mock_vector, patch(
            'app.api.routes.memory_route_story_helpers.schedule_search_rebuild_task',
            return_value='search-task',
        ) as mock_search:
            payload = _import_story_memories_payload(
                db,
                project_id='p1',
                schema_version='story_memory_import_v1',
                items=[
                    StoryMemoryImportV1Item(
                        memory_type='fact',
                        title=' Imported ',
                        content=' imported payload ',
                        importance_score=0.2,
                        story_timeline=40,
                        is_foreshadow=0,
                    )
                ],
                actor_user_id='u_owner',
                request_id='rid-import',
                row_builder=_build_story_memory_import_row,
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

    def test_import_story_memories_payload_rejects_empty_items(self) -> None:
        with self.SessionLocal() as db:
            with self.assertRaises(AppError):
                _import_story_memories_payload(
                    db,
                    project_id='p1',
                    schema_version='story_memory_import_v1',
                    items=[],
                    actor_user_id='u_owner',
                    request_id='rid-import',
                    row_builder=_build_story_memory_import_row,
                )


if __name__ == '__main__':
    unittest.main()
