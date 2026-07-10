from __future__ import annotations

import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.db.utils import new_id
from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
from app.services import prompt_preset_defaults as defaults
from app.services.prompt_preset_resources import PromptPresetResourceBlock, load_preset_resource
from app.services.prompt_presets import LEGACY_IMPORTED_SCOPE
from tests.support import create_tables, make_session_factory, make_sqlite_engine


RESOURCE_KEY = "plan_chapter_v1"


def _add_preset(
    db,
    *,
    project_id: str,
    version: int,
    scope: str = "project",
) -> PromptPreset:
    resource = load_preset_resource(RESOURCE_KEY)
    preset = PromptPreset(
        id=new_id(),
        project_id=project_id,
        name=resource.name,
        resource_key=RESOURCE_KEY,
        category=resource.category,
        scope=scope,
        version=version,
        active_for_json=json.dumps(resource.activation_tasks, ensure_ascii=False),
    )
    db.add(preset)
    db.flush()
    return preset


def _add_block(
    db,
    *,
    preset_id: str,
    resource_block: PromptPresetResourceBlock,
    template: str | None = None,
    identifier: str | None = None,
    origin_template_hash: str | None = None,
    resource_template_outdated: bool | None = None,
) -> PromptBlock:
    block = PromptBlock(
        id=new_id(),
        preset_id=preset_id,
        identifier=identifier or resource_block.identifier,
        name=resource_block.name,
        role=resource_block.role,
        enabled=bool(resource_block.enabled),
        template=str(resource_block.template or "") if template is None else template,
        marker_key=resource_block.marker_key,
        injection_position=resource_block.injection_position,
        injection_depth=resource_block.injection_depth,
        injection_order=int(resource_block.injection_order),
        triggers_json=json.dumps(list(resource_block.triggers or []), ensure_ascii=False),
        forbid_overrides=bool(resource_block.forbid_overrides),
        budget_json=json.dumps(resource_block.budget, ensure_ascii=False) if resource_block.budget else None,
        cache_json=json.dumps(resource_block.cache, ensure_ascii=False) if resource_block.cache else None,
        origin_template_hash=origin_template_hash,
        resource_template_outdated=resource_template_outdated,
    )
    db.add(block)
    return block


def test_upgrade_preserves_user_customized_block_template() -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine)

    resource = load_preset_resource(RESOURCE_KEY)
    resource_block = resource.blocks[0]
    user_template = "USER_CUSTOMIZED_TEMPLATE__SHOULD_SURVIVE_UPGRADE"

    try:
        with factory() as db:
            preset = _add_preset(db, project_id="proj-custom", version=0)
            block = _add_block(
                db,
                preset_id=preset.id,
                resource_block=resource_block,
                template=user_template,
            )
            db.commit()

            defaults.ensure_default_plan_preset(db, project_id=preset.project_id)
            db.refresh(block)
            db.refresh(preset)

            assert block.template == user_template
            assert block.origin_template_hash is None
            assert block.resource_template_outdated is True
            assert preset.version == resource.version
    finally:
        engine.dispose()


def test_tracked_clean_and_dirty_blocks_upgrade_selectively(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine)

    resource = load_preset_resource(RESOURCE_KEY)
    clean_resource_block = resource.blocks[0]
    dirty_resource_block = resource.blocks[1]
    clean_template_v2 = "RESOURCE_DEFAULT_V2_CLEAN"
    dirty_template_v2 = "RESOURCE_DEFAULT_V2_DIRTY"
    upgraded_resource = replace(
        resource,
        version=resource.version + 1,
        blocks=[
            replace(
                clean_resource_block,
                template=clean_template_v2,
                name="RESOURCE_V2_CLEAN_NAME",
                enabled=not clean_resource_block.enabled,
                injection_order=clean_resource_block.injection_order + 10,
            ),
            replace(
                dirty_resource_block,
                template=dirty_template_v2,
                name="RESOURCE_V2_DIRTY_NAME",
                enabled=not dirty_resource_block.enabled,
                injection_order=dirty_resource_block.injection_order + 10,
            ),
        ],
    )
    monkeypatch.setattr(defaults, "load_preset_resource", lambda _key: upgraded_resource)

    try:
        with factory() as db:
            preset = _add_preset(db, project_id="proj-tracked", version=resource.version)
            clean = _add_block(
                db,
                preset_id=preset.id,
                resource_block=clean_resource_block,
                origin_template_hash=defaults.prompt_template_hash(clean_resource_block.template),
                resource_template_outdated=False,
            )
            dirty = _add_block(
                db,
                preset_id=preset.id,
                resource_block=dirty_resource_block,
                template="USER_DIRTY_TEMPLATE",
                origin_template_hash=defaults.prompt_template_hash(dirty_resource_block.template),
                resource_template_outdated=True,
            )
            dirty.name = "USER_CUSTOM_NAME"
            dirty.enabled = False
            db.commit()

            defaults.ensure_default_plan_preset(db, project_id=preset.project_id)
            db.refresh(clean)
            db.refresh(dirty)
            db.refresh(preset)

            assert clean.template == clean_template_v2
            assert clean.name == "RESOURCE_V2_CLEAN_NAME"
            assert clean.enabled is (not clean_resource_block.enabled)
            assert clean.injection_order == clean_resource_block.injection_order + 10
            assert clean.origin_template_hash == defaults.prompt_template_hash(clean_template_v2)
            assert clean.resource_template_outdated is False
            assert dirty.template == "USER_DIRTY_TEMPLATE"
            assert dirty.name == "USER_CUSTOM_NAME"
            assert dirty.enabled is False
            assert dirty.injection_order == dirty_resource_block.injection_order
            assert dirty.origin_template_hash == defaults.prompt_template_hash(dirty_resource_block.template)
            assert dirty.resource_template_outdated is True
            assert preset.version == upgraded_resource.version
    finally:
        engine.dispose()


def test_custom_block_rebases_when_user_restores_current_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine)

    resource = load_preset_resource(RESOURCE_KEY)
    resource_block = resource.blocks[0]
    template_v2 = "RESOURCE_DEFAULT_V2_RESTORED"
    upgraded_resource = replace(
        resource,
        version=resource.version + 1,
        blocks=[replace(resource_block, template=template_v2), *resource.blocks[1:]],
    )
    monkeypatch.setattr(defaults, "load_preset_resource", lambda _key: upgraded_resource)

    try:
        with factory() as db:
            preset = _add_preset(db, project_id="proj-restore", version=resource.version)
            block = _add_block(
                db,
                preset_id=preset.id,
                resource_block=resource_block,
                template="USER_DIRTY_TEMPLATE",
                origin_template_hash=defaults.prompt_template_hash(resource_block.template),
                resource_template_outdated=True,
            )
            db.commit()

            defaults.ensure_default_plan_preset(db, project_id=preset.project_id)
            db.refresh(block)
            assert block.resource_template_outdated is True

            block.template = template_v2
            defaults.refresh_prompt_block_resource_status(block)
            assert block.resource_template_outdated is True
            db.commit()

            defaults.ensure_default_plan_preset(db, project_id=preset.project_id)
            db.refresh(block)

            assert block.template == template_v2
            assert block.origin_template_hash == defaults.prompt_template_hash(template_v2)
            assert block.resource_template_outdated is False
    finally:
        engine.dispose()


def test_same_version_legacy_blocks_are_classified_conservatively() -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine)

    resource = load_preset_resource(RESOURCE_KEY)
    exact_resource_block = resource.blocks[0]
    custom_resource_block = resource.blocks[1]

    try:
        with factory() as db:
            preset = _add_preset(db, project_id="proj-legacy", version=resource.version)
            exact = _add_block(db, preset_id=preset.id, resource_block=exact_resource_block)
            custom = _add_block(
                db,
                preset_id=preset.id,
                resource_block=custom_resource_block,
                template="LEGACY_CUSTOM_TEMPLATE",
            )
            db.commit()

            defaults.ensure_default_plan_preset(db, project_id=preset.project_id)
            db.refresh(exact)
            db.refresh(custom)

            assert exact.origin_template_hash == defaults.prompt_template_hash(exact_resource_block.template)
            assert exact.resource_template_outdated is False
            assert custom.template == "LEGACY_CUSTOM_TEMPLATE"
            assert custom.origin_template_hash is None
            assert custom.resource_template_outdated is True
    finally:
        engine.dispose()


def test_already_new_template_rebases_resource_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine)

    resource = load_preset_resource(RESOURCE_KEY)
    resource_block = resource.blocks[0]
    template_v2 = "RESOURCE_DEFAULT_V2_ALREADY_PRESENT"
    upgraded_resource = replace(
        resource,
        version=resource.version + 1,
        blocks=[replace(resource_block, template=template_v2), *resource.blocks[1:]],
    )
    monkeypatch.setattr(defaults, "load_preset_resource", lambda _key: upgraded_resource)

    try:
        with factory() as db:
            preset = _add_preset(db, project_id="proj-rebase", version=resource.version)
            block = _add_block(
                db,
                preset_id=preset.id,
                resource_block=resource_block,
                template=template_v2,
                origin_template_hash=None,
                resource_template_outdated=None,
            )
            db.commit()

            defaults.ensure_default_plan_preset(db, project_id=preset.project_id)
            db.refresh(block)

            assert block.template == template_v2
            assert block.origin_template_hash == defaults.prompt_template_hash(template_v2)
            assert block.resource_template_outdated is False
    finally:
        engine.dispose()


def test_removed_resource_blocks_delete_only_clean_tracked_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine)

    resource = load_preset_resource(RESOURCE_KEY)
    resource_block = resource.blocks[0]
    upgraded_resource = replace(resource, version=resource.version + 1)
    monkeypatch.setattr(defaults, "load_preset_resource", lambda _key: upgraded_resource)

    try:
        with factory() as db:
            preset = _add_preset(db, project_id="proj-removed", version=resource.version)
            clean = _add_block(
                db,
                preset_id=preset.id,
                resource_block=resource_block,
                identifier="removed.clean",
                template="REMOVED_DEFAULT",
                origin_template_hash=defaults.prompt_template_hash("REMOVED_DEFAULT"),
                resource_template_outdated=False,
            )
            dirty = _add_block(
                db,
                preset_id=preset.id,
                resource_block=resource_block,
                identifier="removed.dirty",
                template="REMOVED_CUSTOM",
                origin_template_hash=defaults.prompt_template_hash("REMOVED_DEFAULT"),
                resource_template_outdated=True,
            )
            unknown = _add_block(
                db,
                preset_id=preset.id,
                resource_block=resource_block,
                identifier="removed.unknown",
                template="UNKNOWN_CUSTOM",
            )
            db.commit()
            clean_id = clean.id

            defaults.ensure_default_plan_preset(db, project_id=preset.project_id)

            assert db.get(PromptBlock, clean_id) is None
            db.refresh(dirty)
            db.refresh(unknown)
            assert dirty.template == "REMOVED_CUSTOM"
            assert dirty.resource_template_outdated is True
            assert unknown.template == "UNKNOWN_CUSTOM"
            assert unknown.origin_template_hash is None
            assert unknown.resource_template_outdated is True
    finally:
        engine.dispose()


def test_renamed_resource_block_is_not_deleted_during_reconciliation(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine)

    resource = load_preset_resource(RESOURCE_KEY)
    resource_block = resource.blocks[0]
    upgraded_resource = replace(resource, version=resource.version + 1)
    monkeypatch.setattr(defaults, "load_preset_resource", lambda _key: upgraded_resource)

    try:
        with factory() as db:
            preset = _add_preset(db, project_id="proj-renamed", version=resource.version)
            block = _add_block(
                db,
                preset_id=preset.id,
                resource_block=resource_block,
                origin_template_hash=defaults.prompt_template_hash(resource_block.template),
                resource_template_outdated=False,
            )
            block.identifier = "user.renamed.resource.block"
            defaults.mark_prompt_block_resource_outdated(block)
            db.commit()
            block_id = block.id

            defaults.ensure_default_plan_preset(db, project_id=preset.project_id)

            renamed = db.get(PromptBlock, block_id)
            assert renamed is not None
            assert renamed.identifier == "user.renamed.resource.block"
            assert renamed.resource_template_outdated is True
            restored_resource_block = (
                db.execute(
                    select(PromptBlock).where(
                        PromptBlock.preset_id == preset.id,
                        PromptBlock.identifier == resource_block.identifier,
                    )
                )
                .scalars()
                .one()
            )
            assert restored_resource_block.id != block_id
            assert restored_resource_block.resource_template_outdated is False
    finally:
        engine.dispose()


def test_new_default_blocks_record_resource_provenance() -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine)

    try:
        with factory() as db:
            preset = defaults.ensure_default_plan_preset(db, project_id="proj-new")
            blocks = db.execute(select(PromptBlock).where(PromptBlock.preset_id == preset.id)).scalars().all()

            assert blocks
            assert all(block.origin_template_hash == defaults.prompt_template_hash(block.template) for block in blocks)
            assert all(block.resource_template_outdated is False for block in blocks)
    finally:
        engine.dispose()


@pytest.mark.parametrize("scope", ["project", LEGACY_IMPORTED_SCOPE])
def test_active_preset_upgrade_failure_is_sanitized_and_recovers_session(
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine)

    try:
        with factory() as db:
            preset = _add_preset(db, project_id=f"proj-failure-{scope}", version=0, scope=scope)
            unrelated = PromptPreset(
                id=new_id(),
                project_id=preset.project_id,
                name="Unrelated original",
                resource_key=None,
                category=None,
                scope="project",
                version=1,
                active_for_json="[]",
            )
            db.add(unrelated)
            db.commit()
            original_version = preset.version
            unrelated.name = "Unrelated pending"

            def _failing_upgrade(session, **_kwargs):
                row = session.get(PromptPreset, preset.id)
                assert row is not None
                row.version = 999
                session.flush()
                raise RuntimeError("secret-token=do-not-log")

            warning_calls: list[tuple[str, tuple[object, ...]]] = []

            def _record_warning(message: str, *args: object) -> None:
                warning_calls.append((message, args))

            monkeypatch.setattr(defaults, "_ensure_default_preset_from_resource", _failing_upgrade)
            monkeypatch.setattr(defaults.logger, "warning", _record_warning)

            selected = defaults.get_active_preset_for_task(
                db,
                project_id=preset.project_id,
                task="plan_chapter",
                allow_autocreate=False,
            )

            assert selected.id == preset.id
            assert selected.version == original_version
            assert unrelated.name == "Unrelated pending"
            assert db.execute(select(PromptPreset).where(PromptPreset.id == preset.id)).scalar_one().version == original_version
            assert len(warning_calls) == 1
            message_format, message_args = warning_calls[0]
            message = message_format % message_args
            assert f"preset_id={preset.id}" in message
            assert f"resource_key={RESOURCE_KEY}" in message
            assert "error_type=RuntimeError" in message
            assert "secret-token" not in message
            assert "do-not-log" not in message
            db.commit()

        with factory() as db:
            assert db.get(PromptPreset, unrelated.id).name == "Unrelated pending"
            assert db.get(PromptPreset, preset.id).version == original_version
    finally:
        engine.dispose()


def test_successful_auto_upgrade_does_not_commit_caller_owned_changes() -> None:
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine)

    resource = load_preset_resource(RESOURCE_KEY)
    resource_block = resource.blocks[0]

    try:
        with factory() as db:
            preset = _add_preset(db, project_id="proj-pending-success", version=0)
            block = _add_block(
                db,
                preset_id=preset.id,
                resource_block=resource_block,
                template="LEGACY_CUSTOM_TEMPLATE",
            )
            unrelated = PromptPreset(
                id=new_id(),
                project_id=preset.project_id,
                name="Unrelated original",
                resource_key=None,
                category=None,
                scope="project",
                version=1,
                active_for_json="[]",
            )
            db.add(unrelated)
            db.commit()
            preset_id = preset.id
            block_id = block.id
            unrelated_id = unrelated.id
            unrelated.name = "Unrelated pending"

            selected = defaults.get_active_preset_for_task(
                db,
                project_id=preset.project_id,
                task="plan_chapter",
                allow_autocreate=False,
            )

            assert selected.version == resource.version
            assert block.resource_template_outdated is True
            assert unrelated.name == "Unrelated pending"
            db.rollback()

        with factory() as db:
            stored_preset = db.get(PromptPreset, preset_id)
            stored_block = db.get(PromptBlock, block_id)
            stored_unrelated = db.get(PromptPreset, unrelated_id)
            assert stored_preset is not None and stored_preset.version == 0
            assert stored_block is not None and stored_block.resource_template_outdated is None
            assert stored_unrelated is not None and stored_unrelated.name == "Unrelated original"
    finally:
        engine.dispose()
