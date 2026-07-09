from __future__ import annotations

import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.knowledge_base import KnowledgeBase
from app.models.llm_preset import LLMPreset
from app.models.outline import Outline
from app.models.project import Project
from app.models.project_membership import ProjectMembership
from app.models.project_settings import ProjectSettings
from app.models.project_source_document import ProjectSourceDocument
from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
from app.models.story_memory import StoryMemory
from app.models.user import User
from app.services.import_export_service import export_project_bundle, import_project_bundle
from app.services.prompt_presets import ensure_default_chapter_preset, ensure_default_outline_preset
from app.services.vector_kb_service import ensure_default_kb


class TestProjectBundleRoundtrip(unittest.TestCase):
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
                Project.__table__,
                ProjectMembership.__table__,
                ProjectSettings.__table__,
                LLMPreset.__table__,
                Outline.__table__,
                Chapter.__table__,
                Character.__table__,
                PromptPreset.__table__,
                PromptBlock.__table__,
                StoryMemory.__table__,
                KnowledgeBase.__table__,
                ProjectSourceDocument.__table__,
            ],
        )

        self.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def test_export_then_import_creates_new_project(self) -> None:
        with self.SessionLocal() as db:
            _seed_project(db)

            bundle = export_project_bundle(db, project_id="p1")
            self.assertEqual(bundle.get("schema_version"), "project_bundle_v1")

            # Security redline: do not export ciphertext.
            settings = bundle.get("settings") or {}
            vec = settings.get("vector_embedding") or {}
            self.assertIn("has_api_key", vec)
            self.assertIn("masked_api_key", vec)
            self.assertNotIn("vector_embedding_api_key_ciphertext", str(bundle))

            imported = import_project_bundle(db, owner_user_id="u1", bundle=bundle, rebuild_vectors=False)
            self.assertTrue(imported.get("ok"))
            new_project_id = imported.get("project_id")
            self.assertTrue(isinstance(new_project_id, str))
            self.assertNotEqual(new_project_id, "p1")

            new_project = db.get(Project, new_project_id)
            self.assertIsNotNone(new_project)
            self.assertEqual(new_project.name, "Project 1")

            # Ensure key data types roundtrip.
            self.assertEqual(_count(db, select(Outline).where(Outline.project_id == new_project_id)), 1)
            self.assertEqual(_count(db, select(Chapter).where(Chapter.project_id == new_project_id)), 1)
            self.assertEqual(_count(db, select(Character).where(Character.project_id == new_project_id)), 1)
            self.assertEqual(_count(db, select(ProjectSourceDocument).where(ProjectSourceDocument.project_id == new_project_id)), 1)
            self.assertEqual(_count(db, select(StoryMemory).where(StoryMemory.project_id == new_project_id)), 1)
            self.assertGreaterEqual(_count(db, select(KnowledgeBase).where(KnowledgeBase.project_id == new_project_id)), 1)

            new_settings = db.get(ProjectSettings, new_project_id)
            self.assertIsNotNone(new_settings)
            self.assertIsNone(new_settings.vector_embedding_api_key_ciphertext)


def _count(db: Session, stmt) -> int:  # type: ignore[no-untyped-def]
    return int(len(db.execute(stmt).scalars().all()))


def _seed_project(db: Session) -> None:
    db.add(User(id="u1", display_name="User 1", is_admin=False))
    project = Project(
        id="p1",
        owner_user_id="u1",
        name="Project 1",
        genre="fantasy",
        logline="x",
        active_outline_id=None,
        llm_profile_id=None,
    )
    db.add(project)
    db.add(ProjectMembership(project_id="p1", user_id="u1", role="owner"))
    db.add(
        ProjectSettings(
            project_id="p1",
            world_setting="world",
            style_guide="style",
            constraints="constraints",
            vector_embedding_provider="openai",
            vector_embedding_model="text-embedding-3-small",
            vector_embedding_api_key_ciphertext="enc:dummy",
            vector_embedding_api_key_masked="sk****1234",
        )
    )
    db.add(LLMPreset(project_id="p1", provider="openai", base_url=None, model="gpt-4o-mini", temperature=0.2))

    outline = Outline(id="o1", project_id="p1", title="Outline 1", content_md="outline", structure_json=None)
    db.add(outline)
    db.add(Chapter(id="c1", project_id="p1", outline_id="o1", number=1, title="Chapter 1", plan="p", content_md="c", summary="s", status="done"))
    project.active_outline_id = "o1"

    db.add(Character(id="char1", project_id="p1", name="Alice", role="hero", profile="p", notes=None))

    db.add(
        StoryMemory(
            id="sm1",
            project_id="p1",
            chapter_id="c1",
            memory_type="note",
            title="t",
            content="c",
            full_context_md=None,
            importance_score=0.5,
            tags_json=None,
            story_timeline=0,
            text_position=-1,
            text_length=0,
            is_foreshadow=0,
            foreshadow_resolved_at_chapter_id=None,
            metadata_json=None,
        )
    )
    db.add(
        ProjectSourceDocument(
            id="d1",
            project_id="p1",
            actor_user_id="u1",
            filename="doc.txt",
            content_type="txt",
            content_text="hello",
            status="done",
            progress=100,
            progress_message="done",
            chunk_count=0,
            kb_id="default",
            vector_ingest_result_json=None,
            worldbook_proposal_json=None,
            story_memory_proposal_json=None,
            error_message=None,
        )
    )
    db.commit()

    ensure_default_outline_preset(db, project_id="p1", activate=True)
    ensure_default_chapter_preset(db, project_id="p1", activate=True)
    ensure_default_kb(db, project_id="p1")


if __name__ == "__main__":
    unittest.main()
