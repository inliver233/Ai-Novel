from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.prompt_management.mappers import _block_to_out, _preset_to_out, _resource_to_out
from app.core.errors import AppError
from app.db.utils import new_id, utc_now
from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
from app.services.prompt_preset_resources import list_available_preset_resources, load_preset_resource
from app.services.prompt_presets import (
    mark_prompt_block_resource_outdated,
    refresh_prompt_block_resource_status,
    reset_prompt_block_to_default_resource,
    reset_prompt_preset_to_default_resource,
)


def _list_prompt_preset_rows(db: Session, *, project_id: str) -> list[PromptPreset]:
    return (
        db.execute(
            select(PromptPreset)
            .where(PromptPreset.project_id == project_id)
            .order_by(PromptPreset.updated_at.desc())
        )
        .scalars()
        .all()
    )


def _list_prompt_block_rows(db: Session, *, preset_id: str) -> list[PromptBlock]:
    return (
        db.execute(
            select(PromptBlock)
            .where(PromptBlock.preset_id == preset_id)
            .order_by(PromptBlock.injection_order.asc())
        )
        .scalars()
        .all()
    )


def _require_prompt_preset(db: Session, *, preset_id: str) -> PromptPreset:
    preset = db.get(PromptPreset, preset_id)
    if preset is None:
        raise AppError.not_found()
    return preset


def _require_prompt_block(db: Session, *, block_id: str) -> PromptBlock:
    block = db.get(PromptBlock, block_id)
    if block is None:
        raise AppError.not_found()
    return block


def _require_prompt_block_with_preset(db: Session, *, block_id: str) -> tuple[PromptBlock, PromptPreset]:
    block = _require_prompt_block(db, block_id=block_id)
    return block, _require_prompt_preset(db, preset_id=block.preset_id)


def _build_prompt_preset_row(*, project_id: str, preset: object) -> PromptPreset:
    return PromptPreset(
        id=new_id(),
        project_id=project_id,
        name=getattr(preset, "name"),
        category=getattr(preset, "category"),
        scope=getattr(preset, "scope"),
        version=getattr(preset, "version"),
        active_for_json=json.dumps(getattr(preset, "active_for", None) or [], ensure_ascii=False),
    )


def _build_prompt_block_row(
    *,
    preset_id: str,
    block: object,
    default_injection_order: int | None = None,
) -> PromptBlock:
    injection_order = getattr(block, "injection_order", None)
    if injection_order is None:
        injection_order = default_injection_order or 0

    budget = getattr(block, "budget", None)
    cache = getattr(block, "cache", None)
    return PromptBlock(
        id=new_id(),
        preset_id=preset_id,
        identifier=getattr(block, "identifier"),
        name=getattr(block, "name"),
        role=getattr(block, "role"),
        enabled=getattr(block, "enabled"),
        template=getattr(block, "template"),
        marker_key=getattr(block, "marker_key"),
        injection_position=getattr(block, "injection_position"),
        injection_depth=getattr(block, "injection_depth"),
        injection_order=int(injection_order),
        triggers_json=json.dumps(getattr(block, "triggers", None) or [], ensure_ascii=False),
        forbid_overrides=getattr(block, "forbid_overrides"),
        budget_json=json.dumps(budget or {}, ensure_ascii=False) if budget else None,
        cache_json=json.dumps(cache or {}, ensure_ascii=False) if cache else None,
    )


def _build_prompt_preset_list_payload(db: Session, *, project_id: str) -> dict[str, object]:
    presets = _list_prompt_preset_rows(db, project_id=project_id)
    return {"presets": [_preset_to_out(preset) for preset in presets]}


def _build_prompt_preset_resources_payload(db: Session, *, project_id: str) -> dict[str, object]:
    presets = db.execute(select(PromptPreset).where(PromptPreset.project_id == project_id)).scalars().all()
    by_resource_key = {str(preset.resource_key): preset for preset in presets if preset.resource_key}
    by_name = {str(preset.name): preset for preset in presets if preset.name}

    resources = []
    for key in list_available_preset_resources():
        resource = load_preset_resource(key)
        preset = by_resource_key.get(key) or by_name.get(resource.name)
        resources.append(_resource_to_out(resource, preset))
    return {"resources": resources}


def _build_prompt_preset_detail_payload(db: Session, *, preset: PromptPreset) -> dict[str, object]:
    blocks = _list_prompt_block_rows(db, preset_id=preset.id)
    return {"preset": _preset_to_out(preset), "blocks": [_block_to_out(block) for block in blocks]}


def _create_prompt_preset_payload(
    db: Session,
    *,
    project_id: str,
    body: object,
) -> dict[str, object]:
    preset = _build_prompt_preset_row(project_id=project_id, preset=body)
    db.add(preset)
    db.commit()
    db.refresh(preset)
    return {"preset": _preset_to_out(preset)}


def _update_prompt_preset_payload(
    db: Session,
    *,
    preset: PromptPreset,
    body: object,
) -> dict[str, object]:
    if getattr(body, "name", None) is not None:
        preset.name = body.name
    if "category" in getattr(body, "model_fields_set", set()):
        preset.category = body.category
    if getattr(body, "scope", None) is not None:
        preset.scope = body.scope
    if getattr(body, "version", None) is not None:
        preset.version = body.version
    if getattr(body, "active_for", None) is not None:
        preset.active_for_json = json.dumps(body.active_for or [], ensure_ascii=False)

    db.commit()
    db.refresh(preset)
    return {"preset": _preset_to_out(preset)}


def _reset_prompt_preset_payload(db: Session, *, preset: PromptPreset) -> dict[str, object]:
    reset_preset = reset_prompt_preset_to_default_resource(db, preset=preset)
    return _build_prompt_preset_detail_payload(db, preset=reset_preset)


def _delete_prompt_preset_payload(db: Session, *, preset: PromptPreset) -> dict[str, object]:
    db.delete(preset)
    db.commit()
    return {}


def _create_prompt_block_payload(
    db: Session,
    *,
    preset: PromptPreset,
    body: object,
) -> dict[str, object]:
    block = _build_prompt_block_row(preset_id=preset.id, block=body)
    db.add(block)
    preset.updated_at = utc_now()
    db.commit()
    db.refresh(block)
    return {"block": _block_to_out(block)}


def _update_prompt_block_payload(
    db: Session,
    *,
    preset: PromptPreset,
    block: PromptBlock,
    body: object,
) -> dict[str, object]:
    metadata_fields = {
        "identifier",
        "name",
        "role",
        "enabled",
        "marker_key",
        "injection_position",
        "injection_depth",
        "injection_order",
        "triggers",
        "forbid_overrides",
        "budget",
        "cache",
    }
    metadata_was_updated = bool(metadata_fields.intersection(getattr(body, "model_fields_set", set())))

    if getattr(body, "identifier", None) is not None:
        block.identifier = body.identifier
    if getattr(body, "name", None) is not None:
        block.name = body.name
    if getattr(body, "role", None) is not None:
        block.role = body.role
    if getattr(body, "enabled", None) is not None:
        block.enabled = body.enabled
    if "template" in getattr(body, "model_fields_set", set()):
        block.template = body.template
        refresh_prompt_block_resource_status(block)
    if "marker_key" in getattr(body, "model_fields_set", set()):
        block.marker_key = body.marker_key
    if getattr(body, "injection_position", None) is not None:
        block.injection_position = body.injection_position
    if "injection_depth" in getattr(body, "model_fields_set", set()):
        block.injection_depth = body.injection_depth
    if getattr(body, "injection_order", None) is not None:
        block.injection_order = body.injection_order
    if "triggers" in getattr(body, "model_fields_set", set()):
        block.triggers_json = (
            json.dumps(body.triggers or [], ensure_ascii=False) if body.triggers is not None else None
        )
    if getattr(body, "forbid_overrides", None) is not None:
        block.forbid_overrides = body.forbid_overrides
    if getattr(body, "budget", None) is not None:
        block.budget_json = json.dumps(body.budget or {}, ensure_ascii=False) if body.budget else None
    if getattr(body, "cache", None) is not None:
        block.cache_json = json.dumps(body.cache or {}, ensure_ascii=False) if body.cache else None
    if metadata_was_updated:
        mark_prompt_block_resource_outdated(block)

    preset.updated_at = utc_now()
    db.commit()
    db.refresh(block)
    return {"block": _block_to_out(block)}


def _reset_prompt_block_payload(
    db: Session,
    *,
    preset: PromptPreset,
    block: PromptBlock,
) -> dict[str, object]:
    reset_block = reset_prompt_block_to_default_resource(db, preset=preset, block=block)
    return {"block": _block_to_out(reset_block)}


def _delete_prompt_block_payload(
    db: Session,
    *,
    preset: PromptPreset,
    block: PromptBlock,
) -> dict[str, object]:
    db.delete(block)
    preset.updated_at = utc_now()
    db.commit()
    return {}


def _reorder_prompt_blocks_payload(
    db: Session,
    *,
    preset: PromptPreset,
    ordered_block_ids: list[str],
) -> dict[str, object]:
    blocks = _list_prompt_block_rows(db, preset_id=preset.id)
    by_id: dict[str, PromptBlock] = {block.id: block for block in blocks}
    existing_ids = [block.id for block in blocks]
    existing_set = set(existing_ids)
    ordered_ids = list(ordered_block_ids or [])

    if len(ordered_ids) != len(existing_ids):
        raise AppError.validation(
            message=f"块顺序（ordered_block_ids）必须包含该 preset 的全部 blocks（expected={len(existing_ids)} got={len(ordered_ids)}）"
        )
    if len(set(ordered_ids)) != len(ordered_ids):
        raise AppError.validation(message="块顺序（ordered_block_ids）包含重复 block_id")

    ordered_set = set(ordered_ids)
    missing = existing_set - ordered_set
    extra = ordered_set - existing_set
    if missing or extra:
        raise AppError.validation(message="块顺序（ordered_block_ids）必须与该 preset 的 blocks 集合完全一致")

    for idx, block_id in enumerate(ordered_ids):
        block = by_id[block_id]
        if block.injection_order != idx:
            block.injection_order = idx
            mark_prompt_block_resource_outdated(block)

    preset.updated_at = utc_now()
    db.commit()

    reordered_blocks = _list_prompt_block_rows(db, preset_id=preset.id)
    return {"blocks": [_block_to_out(block) for block in reordered_blocks]}
