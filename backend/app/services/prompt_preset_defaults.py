from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.utils import new_id, utc_now
from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
from app.services.prompt_preset_resources import list_available_preset_resources, load_preset_resource


def _prompt_block_from_resource(preset_id: str, block_resource: Any) -> PromptBlock:
    triggers_json = json.dumps(list(block_resource.triggers or []), ensure_ascii=False)
    budget_json = json.dumps(block_resource.budget, ensure_ascii=False) if block_resource.budget else None
    cache_json = json.dumps(block_resource.cache, ensure_ascii=False) if block_resource.cache else None
    return PromptBlock(
        id=new_id(),
        preset_id=preset_id,
        identifier=str(block_resource.identifier),
        name=str(block_resource.name),
        role=str(block_resource.role),
        enabled=bool(block_resource.enabled),
        template=str(block_resource.template or ""),
        marker_key=block_resource.marker_key,
        injection_position=str(block_resource.injection_position),
        injection_depth=block_resource.injection_depth,
        injection_order=int(block_resource.injection_order),
        triggers_json=triggers_json,
        forbid_overrides=bool(block_resource.forbid_overrides),
        budget_json=budget_json,
        cache_json=cache_json,
    )


def _ensure_default_preset_from_resource(
    db: Session,
    *,
    project_id: str,
    resource_key: str,
    activate: bool,
) -> PromptPreset:
    from app.services.prompt_presets import parse_json_list

    resource = load_preset_resource(resource_key)

    preset = (
        db.execute(select(PromptPreset).where(PromptPreset.project_id == project_id, PromptPreset.resource_key == resource_key))
        .scalars()
        .first()
    )
    if preset is None:
        preset = (
            db.execute(select(PromptPreset).where(PromptPreset.project_id == project_id, PromptPreset.name == resource.name))
            .scalars()
            .first()
        )

    changed = False
    if preset is not None:
        if not preset.resource_key:
            preset.resource_key = resource_key
            changed = True

        if not preset.category and resource.category:
            preset.category = resource.category
            changed = True

        if activate and resource.activation_tasks:
            active_for = parse_json_list(preset.active_for_json)
            merged = list(dict.fromkeys([*active_for, *resource.activation_tasks]))
            if merged != active_for:
                preset.active_for_json = json.dumps(merged, ensure_ascii=False)
                changed = True

        if int(preset.version or 0) < int(resource.version):
            # Version upgrade: refresh ALL block templates from resource
            blocks_by_identifier = {b.identifier: b for b in resource.blocks}
            existing_blocks = db.execute(select(PromptBlock).where(PromptBlock.preset_id == preset.id)).scalars().all()
            existing_by_identifier = {b.identifier: b for b in existing_blocks}

            # Update existing blocks with new template content
            for res_block in resource.blocks:
                existing = existing_by_identifier.get(res_block.identifier)
                if existing is not None:
                    existing.template = str(res_block.template or "")
                    existing.name = str(res_block.name)
                    existing.role = str(res_block.role)
                    existing.enabled = bool(res_block.enabled)
                    existing.injection_order = int(res_block.injection_order)
                    existing.triggers_json = json.dumps(list(res_block.triggers or []), ensure_ascii=False)
                    existing.budget_json = json.dumps(res_block.budget, ensure_ascii=False) if res_block.budget else None
                else:
                    db.add(_prompt_block_from_resource(preset.id, res_block))

            # Remove blocks that no longer exist in resource
            resource_identifiers = {b.identifier for b in resource.blocks}
            for existing in existing_blocks:
                if existing.identifier not in resource_identifiers:
                    db.delete(existing)

            preset.version = int(resource.version)
            changed = True

        if changed:
            db.commit()
            db.refresh(preset)
        return preset

    preset = PromptPreset(
        id=new_id(),
        project_id=project_id,
        name=resource.name,
        resource_key=resource_key,
        category=resource.category,
        scope=resource.scope,
        version=resource.version,
        active_for_json=json.dumps(resource.activation_tasks if activate else [], ensure_ascii=False),
    )
    db.add(preset)
    db.flush()

    blocks = [_prompt_block_from_resource(preset.id, b) for b in resource.blocks]
    db.add_all(blocks)
    db.commit()
    db.refresh(preset)
    return preset


def ensure_default_plan_preset(db: Session, *, project_id: str, activate: bool = True) -> PromptPreset:
    return _ensure_default_preset_from_resource(
        db,
        project_id=project_id,
        resource_key="plan_chapter_v1",
        activate=activate,
    )


def ensure_default_post_edit_preset(db: Session, *, project_id: str, activate: bool = True) -> PromptPreset:
    return _ensure_default_preset_from_resource(
        db,
        project_id=project_id,
        resource_key="post_edit_v1",
        activate=activate,
    )


def ensure_default_content_optimize_preset(db: Session, *, project_id: str, activate: bool = True) -> PromptPreset:
    return _ensure_default_preset_from_resource(
        db,
        project_id=project_id,
        resource_key="content_optimize_v1",
        activate=activate,
    )


def ensure_default_outline_preset(db: Session, *, project_id: str, activate: bool = False) -> PromptPreset:
    return _ensure_default_preset_from_resource(
        db,
        project_id=project_id,
        resource_key="outline_generate_v3",
        activate=activate,
    )


def ensure_default_chapter_preset(db: Session, *, project_id: str, activate: bool = False) -> PromptPreset:
    return _ensure_default_preset_from_resource(
        db,
        project_id=project_id,
        resource_key="chapter_generate_v4",
        activate=activate,
    )


def ensure_default_detailed_outline_preset(db: Session, *, project_id: str, activate: bool = False) -> PromptPreset:
    return _ensure_default_preset_from_resource(
        db,
        project_id=project_id,
        resource_key="detailed_outline_generate_v1",
        activate=activate,
    )


def resolve_resource_key_for_preset(db: Session, *, preset: PromptPreset) -> str | None:
    if preset.resource_key:
        return str(preset.resource_key)

    name = str(preset.name or "").strip()
    if not name:
        return None

    for key in list_available_preset_resources():
        try:
            resource = load_preset_resource(key)
        except Exception:
            continue
        if resource.name == name:
            preset.resource_key = key
            return key
    return None


def reset_prompt_preset_to_default_resource(db: Session, *, preset: PromptPreset) -> PromptPreset:
    resource_key = resolve_resource_key_for_preset(db, preset=preset)
    if not resource_key:
        raise AppError.validation(message="PromptPreset is not bound to a default resource; reset_to_default is unavailable")

    resource = load_preset_resource(resource_key)

    preset.resource_key = resource_key
    preset.scope = resource.scope
    preset.version = resource.version
    if resource.category:
        preset.category = resource.category
    preset.updated_at = utc_now()

    existing_blocks = db.execute(select(PromptBlock).where(PromptBlock.preset_id == preset.id)).scalars().all()
    for b in existing_blocks:
        db.delete(b)
    db.flush()

    blocks = [_prompt_block_from_resource(preset.id, b) for b in resource.blocks]
    db.add_all(blocks)
    db.commit()
    db.refresh(preset)
    return preset


def reset_prompt_block_to_default_resource(db: Session, *, preset: PromptPreset, block: PromptBlock) -> PromptBlock:
    resource_key = resolve_resource_key_for_preset(db, preset=preset)
    if not resource_key:
        raise AppError.validation(message="PromptPreset is not bound to a default resource; block reset_to_default is unavailable")

    resource = load_preset_resource(resource_key)
    res_block = next((b for b in resource.blocks if b.identifier == block.identifier), None)
    if res_block is None:
        raise AppError.validation(
            message="PromptBlock does not belong to the bound default resource; reset_to_default is unavailable",
            details={"resource": resource_key, "identifier": block.identifier},
        )

    block.identifier = str(res_block.identifier)
    block.name = str(res_block.name)
    block.role = str(res_block.role)
    block.enabled = bool(res_block.enabled)
    block.template = str(res_block.template or "")
    block.marker_key = res_block.marker_key
    block.injection_position = str(res_block.injection_position)
    block.injection_depth = res_block.injection_depth
    block.injection_order = int(res_block.injection_order)
    block.triggers_json = json.dumps(list(res_block.triggers or []), ensure_ascii=False)
    block.forbid_overrides = bool(res_block.forbid_overrides)
    block.budget_json = json.dumps(res_block.budget, ensure_ascii=False) if res_block.budget else None
    block.cache_json = json.dumps(res_block.cache, ensure_ascii=False) if res_block.cache else None

    preset.updated_at = utc_now()
    db.commit()
    db.refresh(block)
    return block


def get_active_preset_for_task(db: Session, *, project_id: str, task: str, allow_autocreate: bool = True) -> PromptPreset:
    from app.services.prompt_presets import LEGACY_IMPORTED_SCOPE, parse_json_list

    presets = (
        db.execute(select(PromptPreset).where(PromptPreset.project_id == project_id).order_by(PromptPreset.updated_at.desc()))
        .scalars()
        .all()
    )

    for preset in presets:
        if (preset.scope or "") == LEGACY_IMPORTED_SCOPE:
            continue
        if task in parse_json_list(preset.active_for_json):
            # Auto-upgrade blocks if resource version is newer
            if preset.resource_key:
                try:
                    resource = load_preset_resource(preset.resource_key)
                    if int(preset.version or 0) < int(resource.version):
                        preset = _ensure_default_preset_from_resource(
                            db, project_id=project_id, resource_key=preset.resource_key, activate=False,
                        )
                except Exception:
                    pass
            return preset

    for preset in presets:
        if (preset.scope or "") != LEGACY_IMPORTED_SCOPE:
            continue
        if task in parse_json_list(preset.active_for_json):
            if preset.resource_key:
                try:
                    resource = load_preset_resource(preset.resource_key)
                    if int(preset.version or 0) < int(resource.version):
                        preset = _ensure_default_preset_from_resource(
                            db, project_id=project_id, resource_key=preset.resource_key, activate=False,
                        )
                except Exception:
                    pass
            return preset

    if allow_autocreate:
        if task == "plan_chapter":
            return ensure_default_plan_preset(db, project_id=project_id)
        if task == "post_edit":
            return ensure_default_post_edit_preset(db, project_id=project_id)
        if task == "content_optimize":
            return ensure_default_content_optimize_preset(db, project_id=project_id)
        if task == "outline_generate":
            return ensure_default_outline_preset(db, project_id=project_id, activate=True)
        if task == "chapter_generate":
            return ensure_default_chapter_preset(db, project_id=project_id, activate=True)
        if task == "detailed_outline_generate":
            return ensure_default_detailed_outline_preset(db, project_id=project_id, activate=True)

    if not allow_autocreate:
        raise AppError.validation(
            message=f"No PromptPreset is configured for task={task}; initialize or activate one in Prompt Studio first"
        )

    if presets:
        return presets[0]

    # Last resort: create a minimal preset so generation won't crash.
    preset = PromptPreset(
        id=new_id(),
        project_id=project_id,
        name=f"Auto-created ({task})",
        scope="project",
        version=1,
        active_for_json=json.dumps([task], ensure_ascii=False),
    )
    db.add(preset)
    db.commit()
    db.refresh(preset)
    return preset
