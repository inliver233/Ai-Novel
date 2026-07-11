from __future__ import annotations

import json
import unittest
from unittest.mock import patch

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
from app.models.project_source_document import ProjectSourceDocument, ProjectSourceDocumentChunk
from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
from app.models.story_memory import StoryMemory
from app.models.user import User
from app.services import import_export_service, vector_rebuild_coordinator
from app.services.import_export_service import export_project_bundle, import_project_bundle
from app.services.prompt_presets import ensure_default_chapter_preset, ensure_default_outline_preset
from app.services.prompt_preset_resources import load_preset_resource
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
                ProjectSourceDocumentChunk.__table__,
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
            self.assertEqual(
                _count(db, select(ProjectSourceDocument).where(ProjectSourceDocument.project_id == new_project_id)), 1
            )
            self.assertEqual(_count(db, select(StoryMemory).where(StoryMemory.project_id == new_project_id)), 1)
            self.assertGreaterEqual(
                _count(db, select(KnowledgeBase).where(KnowledgeBase.project_id == new_project_id)), 1
            )

            new_settings = db.get(ProjectSettings, new_project_id)
            self.assertIsNotNone(new_settings)
            self.assertIsNone(new_settings.vector_embedding_api_key_ciphertext)

    def test_import_baseline_failure_rolls_back_all_rows(self) -> None:
        bundle = {"schema_version": "project_bundle_v1", "project": {"name": "Atomic Import"}}
        with self.SessionLocal() as db:
            db.add(User(id="u1", display_name="User 1", is_admin=False))
            db.commit()
            with (
                patch(
                    "app.services.prompt_preset_defaults.load_preset_resource",
                    side_effect=RuntimeError("baseline failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "baseline failed"),
            ):
                import_project_bundle(db, owner_user_id="u1", bundle=bundle)
            self.assertIsNone(db.execute(select(Project).where(Project.name == "Atomic Import")).scalar_one_or_none())
        with self.SessionLocal() as observer:
            self.assertIsNone(
                observer.execute(select(Project).where(Project.name == "Atomic Import")).scalar_one_or_none()
            )
            self.assertEqual(_count(observer, select(ProjectMembership)), 0)
            self.assertEqual(_count(observer, select(PromptPreset)), 0)
            self.assertEqual(_count(observer, select(PromptBlock)), 0)
            self.assertEqual(_count(observer, select(KnowledgeBase)), 0)

    def test_import_vector_preparation_failure_is_degraded_after_commit(self) -> None:
        bundle = {"schema_version": "project_bundle_v1", "project": {"name": "Vector Safe"}}
        with self.SessionLocal() as db:
            db.add(User(id="u1", display_name="User 1", is_admin=False))
            db.commit()
        with (
            self.SessionLocal() as db,
            patch.object(import_export_service, "SessionLocal", self.SessionLocal),
            patch.object(import_export_service, "rebuild_kb_vectors", side_effect=RuntimeError("vector prepare")),
        ):
            result = import_project_bundle(db, owner_user_id="u1", bundle=bundle, rebuild_vectors=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["vector_rebuild"]["disabled_reason"], "preparation_error")
        with self.SessionLocal() as observer:
            self.assertIsNotNone(observer.get(Project, result["project_id"]))

    def test_import_vector_rebuild_failure_is_structured_per_kb(self) -> None:
        bundle = {"schema_version": "project_bundle_v1", "project": {"name": "Vector Rebuild Safe"}}
        with self.SessionLocal() as db:
            db.add(User(id="u1", display_name="User 1", is_admin=False))
            db.commit()
        with (
            self.SessionLocal() as db,
            patch.object(import_export_service, "SessionLocal", self.SessionLocal),
            patch.object(
                import_export_service,
                "rebuild_kb_vectors",
                return_value={
                    "enabled": True,
                    "skipped": True,
                    "kbs": {
                        "selected": ["default"],
                        "per_kb": {
                            "default": {
                                "enabled": True,
                                "skipped": True,
                                "disabled_reason": "error",
                                "error_type": "RuntimeError",
                                "rebuilt": 0,
                            }
                        },
                    },
                },
            ),
        ):
            result = import_project_bundle(db, owner_user_id="u1", bundle=bundle, rebuild_vectors=True)
        self.assertTrue(result["ok"])
        default_result = result["vector_rebuild"]["kbs"]["per_kb"]["default"]
        self.assertEqual(default_result["disabled_reason"], "error")
        self.assertEqual(default_result["error_type"], "RuntimeError")

    def test_bundle_rebuild_restores_custom_document_to_owning_kb(self) -> None:
        bundle = {
            "schema_version": "project_bundle_v1",
            "project": {"name": "Custom KB Bundle"},
            "knowledge_bases": {
                "kbs": [
                    {"kb_id": "default", "name": "Default"},
                    {"kb_id": "research", "name": "Research"},
                ]
            },
            "source_documents": {
                "docs": [
                    {
                        "filename": "research.txt",
                        "content_type": "txt",
                        "content_text": "custom bundle evidence",
                        "kb_id": "research",
                    }
                ]
            },
        }
        writes: dict[str, list] = {}

        def _write(*, project_id, kb_id, chunks, embeddings):  # type: ignore[no-untyped-def]
            writes[str(kb_id)] = list(chunks)
            return {
                "enabled": True,
                "skipped": False,
                "rebuilt": len(chunks),
                "ingested": len(chunks),
                "backend": "test",
            }

        with self.SessionLocal() as db:
            db.add(User(id="u1", display_name="User 1", is_admin=False))
            db.commit()
        with (
            self.SessionLocal() as db,
            patch.object(import_export_service, "SessionLocal", self.SessionLocal),
            patch.object(vector_rebuild_coordinator, "_vector_enabled_reason", return_value=(True, None)),
            patch.object(
                vector_rebuild_coordinator,
                "embed_texts_with_providers",
                return_value={"enabled": True, "vectors": [[1.0]]},
            ),
            patch.object(vector_rebuild_coordinator, "rebuild_project_with_embeddings", side_effect=_write),
        ):
            result = import_project_bundle(db, owner_user_id="u1", bundle=bundle, rebuild_vectors=True)

        self.assertTrue(result["ok"])
        self.assertEqual(writes["default"], [])
        self.assertEqual([chunk.text for chunk in writes["research"]], ["custom bundle evidence"])
        self.assertEqual(writes["research"][0].metadata["knowledge_base_id"], "research")

    def test_import_preserves_custom_active_and_modified_builtin(self) -> None:
        bundle = {
            "schema_version": "project_bundle_v1",
            "project": {"name": "Prompt Import"},
            "prompt_presets": {
                "presets": [
                    {
                        "preset": {
                            "name": "Modified Chapter",
                            "resource_key": "chapter_generate_v4",
                            "version": 1,
                            "active_for": ["chapter_generate"],
                        },
                        "blocks": [{"identifier": "custom_chapter", "name": "Custom", "template": "KEEP ME"}],
                    },
                    {
                        "preset": {"name": "Custom Plan", "version": 7, "active_for": ["plan_chapter"]},
                        "blocks": [{"identifier": "custom_plan", "name": "Plan", "template": "CUSTOM PLAN"}],
                    },
                ]
            },
        }
        with self.SessionLocal() as db:
            db.add(User(id="u1", display_name="User 1", is_admin=False))
            db.commit()
            result = import_project_bundle(db, owner_user_id="u1", bundle=bundle)
        with self.SessionLocal() as observer:
            rows = (
                observer.execute(select(PromptPreset).where(PromptPreset.project_id == result["project_id"]))
                .scalars()
                .all()
            )
            self.assertEqual(len(rows), 7)
            chapter = next(row for row in rows if row.resource_key == "chapter_generate_v4")
            self.assertEqual(chapter.version, 1)
            block = observer.execute(select(PromptBlock).where(PromptBlock.preset_id == chapter.id)).scalar_one()
            self.assertEqual(block.template, "KEEP ME")
            custom = next(row for row in rows if row.name == "Custom Plan")
            self.assertEqual(json.loads(custom.active_for_json), ["plan_chapter"])
            plan_builtin = next(row for row in rows if row.resource_key == "plan_chapter_v1")
            self.assertNotIn("plan_chapter", json.loads(plan_builtin.active_for_json or "[]"))

    def test_import_preserves_same_name_legacy_and_adds_resource_builtin(self) -> None:
        resource = load_preset_resource("plan_chapter_v1")
        bundle = {
            "schema_version": "project_bundle_v1",
            "project": {"name": "Legacy Prompt Import"},
            "prompt_presets": {
                "presets": [
                    {
                        "preset": {"name": resource.name, "version": 77, "active_for": []},
                        "blocks": [{"identifier": "legacy", "name": "Legacy", "template": "DO NOT CHANGE"}],
                    }
                ]
            },
        }
        with self.SessionLocal() as db:
            db.add(User(id="u1", display_name="User 1", is_admin=False))
            db.commit()
            result = import_project_bundle(db, owner_user_id="u1", bundle=bundle)
        with self.SessionLocal() as observer:
            rows = (
                observer.execute(select(PromptPreset).where(PromptPreset.project_id == result["project_id"]))
                .scalars()
                .all()
            )
            legacy = next(row for row in rows if row.resource_key is None)
            self.assertEqual((legacy.name, legacy.version, json.loads(legacy.active_for_json)), (resource.name, 77, []))
            legacy_block = observer.execute(select(PromptBlock).where(PromptBlock.preset_id == legacy.id)).scalar_one()
            self.assertEqual((legacy_block.identifier, legacy_block.template), ("legacy", "DO NOT CHANGE"))
            builtins = [row for row in rows if row.resource_key]
            self.assertEqual(len({row.resource_key for row in builtins}), 6)
            plan_builtin = next(row for row in builtins if row.resource_key == "plan_chapter_v1")
            self.assertIn("plan_chapter", json.loads(plan_builtin.active_for_json or "[]"))


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
    db.add(
        Chapter(
            id="c1",
            project_id="p1",
            outline_id="o1",
            number=1,
            title="Chapter 1",
            plan="p",
            content_md="c",
            summary="s",
            status="done",
        )
    )
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
