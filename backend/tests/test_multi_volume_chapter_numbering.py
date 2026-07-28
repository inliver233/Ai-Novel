from __future__ import annotations

import json
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.errors import AppError
from app.db.base import Base
from app.models.chapter import Chapter
from app.models.detailed_outline import DetailedOutline
from app.models.llm_profile import LLMProfile
from app.models.outline import Outline
from app.models.project import Project
from app.models.user import User
from app.services.chapter_skeleton_generation.stream_service import _create_chapter_records
from app.services.detailed_outline_generation.app_service import create_chapters_from_detailed_outline


class TestMultiVolumeChapterNumbering(unittest.TestCase):
    def setUp(self) -> None:
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
                LLMProfile.__table__,
                Project.__table__,
                Outline.__table__,
                DetailedOutline.__table__,
                Chapter.__table__,
            ],
        )
        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        with self.SessionLocal() as db:
            db.add(User(id="u1", display_name="user"))
            outline = Outline(id="o1", project_id="p1", title="Outline 1", content_md="# Outline")
            project = Project(id="p1", owner_user_id="u1", active_outline_id="o1", name="Project 1")
            db.add(project)
            db.add(outline)
            db.commit()

    def test_create_chapters_from_detailed_outline_applies_offset_and_scoped_replace(self) -> None:
        with self.SessionLocal() as db:
            volume_1 = DetailedOutline(
                id="do-1",
                project_id="p1",
                outline_id="o1",
                volume_number=1,
                volume_title="卷一",
                structure_json=json.dumps(
                    {
                        "chapters": [
                            {"number": 1, "title": "卷一-1"},
                            {"number": 2, "title": "卷一-2"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                status="planned",
            )
            volume_2 = DetailedOutline(
                id="do-2",
                project_id="p1",
                outline_id="o1",
                volume_number=2,
                volume_title="卷二",
                structure_json=json.dumps(
                    {
                        "chapters": [
                            {"number": 1, "title": "卷二-1"},
                            {"number": 2, "title": "卷二-2"},
                            {"number": 3, "title": "卷二-3"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                status="planned",
            )
            db.add_all([volume_1, volume_2])
            db.commit()

            created_v1 = create_chapters_from_detailed_outline("do-1", db)
            self.assertEqual([item["number"] for item in created_v1], [1, 2])

            created_v2 = create_chapters_from_detailed_outline("do-2", db)
            self.assertEqual([item["number"] for item in created_v2], [3, 4, 5])

            volume_2 = db.get(DetailedOutline, "do-2")
            self.assertIsNotNone(volume_2)
            assert volume_2 is not None
            volume_2.structure_json = json.dumps(
                {
                    "chapters": [
                        {"number": 1, "title": "卷二-新1"},
                        {"number": 2, "title": "卷二-新2"},
                    ]
                },
                ensure_ascii=False,
            )
            db.commit()

            replaced_v2 = create_chapters_from_detailed_outline("do-2", db, replace=True)
            self.assertEqual([item["number"] for item in replaced_v2], [3, 4])

            stored = db.execute(
                select(Chapter).where(Chapter.outline_id == "o1").order_by(Chapter.number)
            ).scalars().all()
            self.assertEqual([chapter.number for chapter in stored], [1, 2, 3, 4])
            self.assertEqual([chapter.title for chapter in stored], ["卷一-1", "卷一-2", "卷二-新1", "卷二-新2"])

    def test_create_chapters_from_detailed_outline_conflict_message_uses_volume_range(self) -> None:
        with self.SessionLocal() as db:
            db.add_all(
                [
                    DetailedOutline(
                        id="do-1",
                        project_id="p1",
                        outline_id="o1",
                        volume_number=1,
                        volume_title="卷一",
                        structure_json=json.dumps({"chapters": [{"number": 1}, {"number": 2}]}, ensure_ascii=False),
                        status="planned",
                    ),
                    DetailedOutline(
                        id="do-2",
                        project_id="p1",
                        outline_id="o1",
                        volume_number=2,
                        volume_title="卷二",
                        structure_json=json.dumps({"chapters": [{"number": 1}, {"number": 2}]}, ensure_ascii=False),
                        status="planned",
                    ),
                ]
            )
            db.commit()

            create_chapters_from_detailed_outline("do-1", db)
            create_chapters_from_detailed_outline("do-2", db)

            with self.assertRaises(AppError) as ctx:
                create_chapters_from_detailed_outline("do-2", db)

            self.assertEqual(ctx.exception.code, "CONFLICT")
            self.assertIn("编号3-4", ctx.exception.message)

    def test_create_chapters_from_detailed_outline_uses_count_for_offset(self) -> None:
        """Offset uses len() (count of chapters) not max() of raw numbers.

        Volume 1 has 2 chapters (raw numbers 1, 3 -- sparse).  With count-based
        offset the offset for volume 2 is 2, and the normalized local numbers
        for volume 1 are sequential 1, 2 (not raw 1, 3).
        """
        with self.SessionLocal() as db:
            db.add_all(
                [
                    DetailedOutline(
                        id="do-1",
                        project_id="p1",
                        outline_id="o1",
                        volume_number=1,
                        volume_title="卷一",
                        structure_json=json.dumps(
                            {"chapters": [{"number": 1, "title": "卷一-1"}, {"number": 3, "title": "卷一-3"}]},
                            ensure_ascii=False,
                        ),
                        status="planned",
                    ),
                    DetailedOutline(
                        id="do-2",
                        project_id="p1",
                        outline_id="o1",
                        volume_number=2,
                        volume_title="卷二",
                        structure_json=json.dumps(
                            {"chapters": [{"number": 1, "title": "卷二-1"}]},
                            ensure_ascii=False,
                        ),
                        status="planned",
                    ),
                ]
            )
            db.commit()

            # Volume 1: raw [1,3] -> normalized local [1,2] -> global [1,2]
            created_v1 = create_chapters_from_detailed_outline("do-1", db)
            self.assertEqual([item["number"] for item in created_v1], [1, 2])

            # Volume 2: offset=2 (count of vol1 chapters), local [1] -> global [3]
            created_v2 = create_chapters_from_detailed_outline("do-2", db)
            self.assertEqual([item["number"] for item in created_v2], [3])

    def test_create_chapters_from_detailed_outline_replace_uses_normalized_numbers(self) -> None:
        """Replace with sparse raw numbers [1, 3] normalizes to sequential [1, 2].

        Volume 1 is the last volume (no later volumes), so the replace logic
        also deletes all chapters >= min(target_numbers).  With target_numbers
        = {1, 2}, all existing chapters (1, 2, 3) are deleted and only the new
        two are created.
        """
        with self.SessionLocal() as db:
            db.add(
                DetailedOutline(
                    id="do-1",
                    project_id="p1",
                    outline_id="o1",
                    volume_number=1,
                    volume_title="卷一",
                    structure_json=json.dumps(
                        {"chapters": [{"number": 1, "title": "卷一-新1"}, {"number": 3, "title": "卷一-新3"}]},
                        ensure_ascii=False,
                    ),
                    status="planned",
                )
            )
            db.add_all(
                [
                    Chapter(id="c-1", project_id="p1", outline_id="o1", number=1, title="旧1", plan="", status="planned"),
                    Chapter(id="c-2", project_id="p1", outline_id="o1", number=2, title="保留2", plan="", status="planned"),
                    Chapter(id="c-3", project_id="p1", outline_id="o1", number=3, title="旧3", plan="", status="planned"),
                ]
            )
            db.commit()

            # raw [1,3] -> normalized target_numbers = {1,2}
            # Last volume + contiguous set -> also deletes chapters >= min({1,2}) = 1
            created = create_chapters_from_detailed_outline("do-1", db, replace=True)
            self.assertEqual([item["number"] for item in created], [1, 2])

            stored = db.execute(
                select(Chapter).where(Chapter.outline_id == "o1").order_by(Chapter.number)
            ).scalars().all()
            self.assertEqual([chapter.number for chapter in stored], [1, 2])
            self.assertEqual([chapter.title for chapter in stored], ["卷一-新1", "卷一-新3"])

    def test_create_chapter_records_applies_offset_when_not_replacing(self) -> None:
        with self.SessionLocal() as db:
            detailed_outline = DetailedOutline(
                id="do-2",
                project_id="p1",
                outline_id="o1",
                volume_number=2,
                volume_title="卷二",
                structure_json=None,
                status="planned",
            )
            db.add(detailed_outline)
            db.add(
                Chapter(
                    id="c-existing",
                    project_id="p1",
                    outline_id="o1",
                    number=3,
                    title="已有章节",
                    plan="",
                    status="planned",
                )
            )
            db.commit()

            created = _create_chapter_records(
                db,
                detailed_outline,
                [{"number": 1, "title": "卷二-1"}, {"number": 2, "title": "卷二-2"}],
                replace=False,
                chapter_offset=2,
            )

            self.assertEqual([item["number"] for item in created], [4])
            stored = db.execute(
                select(Chapter).where(Chapter.outline_id == "o1").order_by(Chapter.number)
            ).scalars().all()
            self.assertEqual([chapter.number for chapter in stored], [3, 4])

    def test_create_chapter_records_replace_removes_previous_volume_numbers(self) -> None:
        with self.SessionLocal() as db:
            detailed_outline = DetailedOutline(
                id="do-2",
                project_id="p1",
                outline_id="o1",
                volume_number=2,
                volume_title="卷二",
                structure_json=None,
                status="planned",
            )
            db.add(detailed_outline)
            db.add_all(
                [
                    Chapter(id="c-3", project_id="p1", outline_id="o1", number=3, title="旧3", plan="", status="planned"),
                    Chapter(id="c-4", project_id="p1", outline_id="o1", number=4, title="旧4", plan="", status="planned"),
                    Chapter(id="c-5", project_id="p1", outline_id="o1", number=5, title="旧5", plan="", status="planned"),
                ]
            )
            db.commit()

            created = _create_chapter_records(
                db,
                detailed_outline,
                [{"number": 1, "title": "卷二-1"}, {"number": 2, "title": "卷二-2"}],
                replace=True,
                chapter_offset=2,
                replace_numbers={3, 4, 5},
            )

            self.assertEqual([item["number"] for item in created], [3, 4])
            stored = db.execute(
                select(Chapter).where(Chapter.outline_id == "o1").order_by(Chapter.number)
            ).scalars().all()
            self.assertEqual([chapter.number for chapter in stored], [3, 4])
            self.assertEqual([chapter.title for chapter in stored], ["卷二-1", "卷二-2"])


    def test_cross_volume_no_gap_with_global_raw_numbers(self) -> None:
        """Regression test: LLM generates global numbers (7,8,9...) for volume 2.

        Volume 1 has 6 chapters (raw 1-6).  LLM is told to start at chapter 7,
        so structure_json stores raw numbers 7,8,9.  With max()-based offset
        this would produce offset=6+9=15 for volume 3 (double-counting).
        With count-based offset: offset=6 for vol2, vol2 chapters get global
        7,8,9 via sequential local numbering (1,2,3) + offset 6.
        """
        with self.SessionLocal() as db:
            db.add_all(
                [
                    DetailedOutline(
                        id="do-1",
                        project_id="p1",
                        outline_id="o1",
                        volume_number=1,
                        volume_title="卷一",
                        structure_json=json.dumps(
                            {
                                "chapters": [
                                    {"number": i, "title": f"卷一-{i}"}
                                    for i in range(1, 7)
                                ]
                            },
                            ensure_ascii=False,
                        ),
                        status="planned",
                    ),
                    DetailedOutline(
                        id="do-2",
                        project_id="p1",
                        outline_id="o1",
                        volume_number=2,
                        volume_title="卷二",
                        structure_json=json.dumps(
                            {
                                "chapters": [
                                    {"number": 7, "title": "卷二-7"},
                                    {"number": 8, "title": "卷二-8"},
                                    {"number": 9, "title": "卷二-9"},
                                ]
                            },
                            ensure_ascii=False,
                        ),
                        status="planned",
                    ),
                ]
            )
            db.commit()

            created_v1 = create_chapters_from_detailed_outline("do-1", db)
            self.assertEqual([item["number"] for item in created_v1], [1, 2, 3, 4, 5, 6])

            created_v2 = create_chapters_from_detailed_outline("do-2", db)
            # offset = 6 (count of vol1 chapters), local [1,2,3] -> global [7,8,9]
            self.assertEqual([item["number"] for item in created_v2], [7, 8, 9])

    def test_create_chapter_records_normalized_numbering(self) -> None:
        """_create_chapter_records normalizes raw numbers to sequential local indices."""
        with self.SessionLocal() as db:
            detailed_outline = DetailedOutline(
                id="do-2",
                project_id="p1",
                outline_id="o1",
                volume_number=2,
                volume_title="卷二",
                structure_json=None,
                status="planned",
            )
            db.add(detailed_outline)
            db.commit()

            # Raw numbers 7,8,9 (global numbers from LLM) should normalize to local 1,2,3
            created = _create_chapter_records(
                db,
                detailed_outline,
                [
                    {"number": 7, "title": "卷二-7"},
                    {"number": 8, "title": "卷二-8"},
                    {"number": 9, "title": "卷二-9"},
                ],
                replace=True,
                chapter_offset=6,
            )

            # offset=6, local [1,2,3] -> global [7,8,9]
            self.assertEqual([item["number"] for item in created], [7, 8, 9])

    def _seed_two_volumes(self, db, *, volume_1_structure: str | None) -> None:
        db.add_all(
            [
                DetailedOutline(
                    id="do-1",
                    project_id="p1",
                    outline_id="o1",
                    volume_number=1,
                    volume_title="卷一",
                    structure_json=volume_1_structure,
                    status="planned",
                ),
                DetailedOutline(
                    id="do-2",
                    project_id="p1",
                    outline_id="o1",
                    volume_number=2,
                    volume_title="卷二",
                    structure_json=json.dumps(
                        {
                            "chapters": [
                                {"number": 1, "title": "卷二-1"},
                                {"number": 2, "title": "卷二-2"},
                            ]
                        },
                        ensure_ascii=False,
                    ),
                    status="planned",
                ),
            ]
        )
        db.commit()

    def test_out_of_order_generation_blocked_when_earlier_volume_has_no_structure(self) -> None:
        """backend-generation#5：前置卷 structure=None 时禁止静默 offset=0 乱序建章。"""
        with self.SessionLocal() as db:
            self._seed_two_volumes(db, volume_1_structure=None)

            with self.assertRaises(AppError) as ctx:
                create_chapters_from_detailed_outline("do-2", db)
            self.assertEqual(ctx.exception.code, "EARLIER_VOLUME_NO_CHAPTERS")
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(ctx.exception.details.get("earlier_volume_numbers"), [1])

            db.rollback()
            stored = db.execute(select(Chapter)).scalars().all()
            self.assertEqual(stored, [])

    def test_out_of_order_generation_blocked_when_earlier_volume_structure_unparseable(self) -> None:
        with self.SessionLocal() as db:
            self._seed_two_volumes(db, volume_1_structure="{not-json")

            with self.assertRaises(AppError) as ctx:
                create_chapters_from_detailed_outline("do-2", db)
            self.assertEqual(ctx.exception.code, "EARLIER_VOLUME_NO_CHAPTERS")
            self.assertEqual(ctx.exception.details.get("earlier_volume_numbers"), [1])

            db.rollback()
            self.assertEqual(db.execute(select(Chapter)).scalars().all(), [])

    def test_shared_offset_helper_counts_valid_earlier_volumes(self) -> None:
        from app.services.chapter_numbering import compute_chapter_offset

        with self.SessionLocal() as db:
            self._seed_two_volumes(
                db,
                volume_1_structure=json.dumps(
                    {"chapters": [{"number": 1, "title": "a"}, {"number": 2, "title": "b"}]},
                    ensure_ascii=False,
                ),
            )
            volume_2 = db.get(DetailedOutline, "do-2")
            assert volume_2 is not None
            self.assertEqual(compute_chapter_offset(db, volume_2), 2)


if __name__ == "__main__":
    unittest.main()
