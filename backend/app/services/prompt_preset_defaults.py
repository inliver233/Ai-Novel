from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.utils import new_id, utc_now
from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
from app.models.project import Project
from app.services.prompt_preset_resources import list_available_preset_resources, load_preset_resource


_BUILTIN_PROMPT_DEFAULTS: tuple[tuple[str, bool], ...] = (
    ("plan_chapter_v1", True),
    ("post_edit_v1", True),
    ("content_optimize_v1", True),
    ("outline_generate_v3", False),
    ("chapter_generate_v4", False),
    ("detailed_outline_generate_v1", False),
)
_BUILTIN_RESOURCE_UNIQUE_CONSTRAINT = "uq_prompt_presets_project_resource_key"


def _is_builtin_resource_unique_conflict(exc: IntegrityError) -> bool:
    original = exc.orig
    diagnostic = getattr(original, "diag", None)
    if getattr(diagnostic, "constraint_name", None) == _BUILTIN_RESOURCE_UNIQUE_CONSTRAINT:
        return True
    message = str(original).lower()
    return "prompt_presets.project_id" in message and "prompt_presets.resource_key" in message and "unique" in message


def prompt_template_hash(template: str | None) -> str:
    return hashlib.sha256(str(template or "").encode("utf-8")).hexdigest()


def refresh_prompt_block_resource_status(block: PromptBlock) -> None:
    if block.origin_template_hash is None:
        return
    block.resource_template_outdated = prompt_template_hash(block.template) != block.origin_template_hash


def mark_prompt_block_resource_outdated(block: PromptBlock) -> None:
    if block.origin_template_hash is not None:
        block.resource_template_outdated = True


def _resource_block_metadata_matches(block: PromptBlock, block_resource: Any) -> bool:
    triggers_json = json.dumps(list(block_resource.triggers or []), ensure_ascii=False)
    budget_json = json.dumps(block_resource.budget, ensure_ascii=False) if block_resource.budget else None
    cache_json = json.dumps(block_resource.cache, ensure_ascii=False) if block_resource.cache else None
    return (
        block.identifier == str(block_resource.identifier)
        and block.name == str(block_resource.name)
        and block.role == str(block_resource.role)
        and block.enabled is bool(block_resource.enabled)
        and block.marker_key == block_resource.marker_key
        and block.injection_position == str(block_resource.injection_position)
        and block.injection_depth == block_resource.injection_depth
        and block.injection_order == int(block_resource.injection_order)
        and block.triggers_json == triggers_json
        and block.forbid_overrides is bool(block_resource.forbid_overrides)
        and block.budget_json == budget_json
        and block.cache_json == cache_json
    )


def _apply_resource_block_metadata(block: PromptBlock, block_resource: Any) -> bool:
    before = (
        block.identifier,
        block.name,
        block.role,
        block.enabled,
        block.marker_key,
        block.injection_position,
        block.injection_depth,
        block.injection_order,
        block.triggers_json,
        block.forbid_overrides,
        block.budget_json,
        block.cache_json,
    )
    block.identifier = str(block_resource.identifier)
    block.name = str(block_resource.name)
    block.role = str(block_resource.role)
    block.enabled = bool(block_resource.enabled)
    block.marker_key = block_resource.marker_key
    block.injection_position = str(block_resource.injection_position)
    block.injection_depth = block_resource.injection_depth
    block.injection_order = int(block_resource.injection_order)
    block.triggers_json = json.dumps(list(block_resource.triggers or []), ensure_ascii=False)
    block.forbid_overrides = bool(block_resource.forbid_overrides)
    block.budget_json = json.dumps(block_resource.budget, ensure_ascii=False) if block_resource.budget else None
    block.cache_json = json.dumps(block_resource.cache, ensure_ascii=False) if block_resource.cache else None
    after = (
        block.identifier,
        block.name,
        block.role,
        block.enabled,
        block.marker_key,
        block.injection_position,
        block.injection_depth,
        block.injection_order,
        block.triggers_json,
        block.forbid_overrides,
        block.budget_json,
        block.cache_json,
    )
    return before != after


def _prompt_block_from_resource(preset_id: str, block_resource: Any) -> PromptBlock:
    triggers_json = json.dumps(list(block_resource.triggers or []), ensure_ascii=False)
    budget_json = json.dumps(block_resource.budget, ensure_ascii=False) if block_resource.budget else None
    cache_json = json.dumps(block_resource.cache, ensure_ascii=False) if block_resource.cache else None
    template = str(block_resource.template or "")
    return PromptBlock(
        id=new_id(),
        preset_id=preset_id,
        identifier=str(block_resource.identifier),
        name=str(block_resource.name),
        role=str(block_resource.role),
        enabled=bool(block_resource.enabled),
        template=template,
        marker_key=block_resource.marker_key,
        injection_position=str(block_resource.injection_position),
        injection_depth=block_resource.injection_depth,
        injection_order=int(block_resource.injection_order),
        triggers_json=triggers_json,
        forbid_overrides=bool(block_resource.forbid_overrides),
        budget_json=budget_json,
        cache_json=cache_json,
        origin_template_hash=prompt_template_hash(template),
        resource_template_outdated=False,
    )


def _reconcile_resource_blocks(
    db: Session,
    *,
    preset: PromptPreset,
    resource: Any,
    existing_blocks: list[PromptBlock],
) -> bool:
    changed = False
    existing_by_identifier = {block.identifier: block for block in existing_blocks}

    for resource_block in resource.blocks:
        existing = existing_by_identifier.get(resource_block.identifier)
        if existing is None:
            db.add(_prompt_block_from_resource(preset.id, resource_block))
            changed = True
            continue

        before = (
            existing.template,
            existing.origin_template_hash,
            existing.resource_template_outdated,
        )
        resource_template = str(resource_block.template or "")
        resource_hash = prompt_template_hash(resource_template)
        current_hash = prompt_template_hash(existing.template)
        metadata_matches = _resource_block_metadata_matches(existing, resource_block)
        was_trusted_clean = (
            existing.resource_template_outdated is False
            and existing.origin_template_hash is not None
            and current_hash == existing.origin_template_hash
        )
        metadata_changed = False

        if current_hash == resource_hash:
            existing.origin_template_hash = resource_hash
            if was_trusted_clean:
                metadata_changed = _apply_resource_block_metadata(existing, resource_block)
                existing.resource_template_outdated = False
            else:
                existing.resource_template_outdated = not metadata_matches
        elif was_trusted_clean:
            existing.template = resource_template
            metadata_changed = _apply_resource_block_metadata(existing, resource_block)
            existing.origin_template_hash = resource_hash
            existing.resource_template_outdated = False
        else:
            existing.resource_template_outdated = True

        after = (
            existing.template,
            existing.origin_template_hash,
            existing.resource_template_outdated,
        )
        changed = changed or metadata_changed or before != after

    resource_identifiers = {block.identifier for block in resource.blocks}
    for existing in existing_blocks:
        if existing.identifier in resource_identifiers:
            continue
        current_hash = prompt_template_hash(existing.template)
        if (
            existing.resource_template_outdated is False
            and existing.origin_template_hash is not None
            and current_hash == existing.origin_template_hash
        ):
            db.delete(existing)
            changed = True
        else:
            if existing.resource_template_outdated is not True:
                changed = True
            existing.resource_template_outdated = True
    return changed


def _ensure_default_preset_from_resource(
    db: Session,
    *,
    project_id: str,
    resource_key: str,
    activate: bool,
    commit: bool = True,
) -> PromptPreset:
    from app.services.prompt_presets import parse_json_list

    resource = load_preset_resource(resource_key)

    preset = (
        db.execute(
            select(PromptPreset).where(PromptPreset.project_id == project_id, PromptPreset.resource_key == resource_key)
        )
        .scalars()
        .first()
    )
    if preset is None:
        preset = (
            db.execute(
                select(PromptPreset).where(PromptPreset.project_id == project_id, PromptPreset.name == resource.name)
            )
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

        existing_blocks = db.execute(select(PromptBlock).where(PromptBlock.preset_id == preset.id)).scalars().all()
        existing_identifiers = {block.identifier for block in existing_blocks}
        resource_identifiers = {block.identifier for block in resource.blocks}
        resource_version_is_newer = int(preset.version or 0) < int(resource.version)
        provenance_needs_reconcile = any(block.resource_template_outdated is not False for block in existing_blocks)
        resource_block_is_missing = not resource_identifiers.issubset(existing_identifiers)
        if resource_version_is_newer or provenance_needs_reconcile or resource_block_is_missing:
            blocks_changed = _reconcile_resource_blocks(
                db,
                preset=preset,
                resource=resource,
                existing_blocks=existing_blocks,
            )
            changed = changed or blocks_changed

        if resource_version_is_newer:
            preset.version = int(resource.version)
            changed = True

        if changed:
            if commit:
                db.commit()
                db.refresh(preset)
            else:
                db.flush()
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
    if commit:
        db.commit()
        db.refresh(preset)
    else:
        db.flush()
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


def stage_missing_builtin_prompt_defaults(db: Session, *, project_id: str) -> list[PromptPreset]:
    """Stage a complete builtin baseline without mutating imported presets.

    Existing builtins are deliberately left untouched (including their version
    and blocks).  A newly-created builtin is activated only for tasks that are
    not already claimed by any preset, so imported/custom active choices win.
    The caller owns the transaction.
    """
    from app.services.prompt_presets import parse_json_list

    existing = db.execute(select(PromptPreset).where(PromptPreset.project_id == project_id)).scalars().all()
    existing_keys = {str(row.resource_key) for row in existing if row.resource_key}
    claimed_tasks = {task for row in existing for task in parse_json_list(row.active_for_json)}
    staged: list[PromptPreset] = []
    for resource_key, _activate in _BUILTIN_PROMPT_DEFAULTS:
        if resource_key in existing_keys:
            continue
        resource = load_preset_resource(resource_key)
        # Do not use the normal ensure path here: its legacy name fallback is
        # intentionally useful for upgrades, but baseline staging must preserve
        # same-name imports that have no resource_key.
        row = PromptPreset(
            id=new_id(),
            project_id=project_id,
            name=resource.name,
            resource_key=resource_key,
            category=resource.category,
            scope=resource.scope,
            version=resource.version,
            active_for_json="[]",
        )
        db.add(row)
        db.flush()
        db.add_all([_prompt_block_from_resource(row.id, block) for block in resource.blocks])
        activation_tasks = [task for task in resource.activation_tasks if task not in claimed_tasks]
        row.active_for_json = json.dumps(activation_tasks, ensure_ascii=False)
        claimed_tasks.update(activation_tasks)
        staged.append(row)
    db.flush()
    return staged


def sync_builtin_prompt_defaults(db: Session, *, project_id: str) -> list[PromptPreset]:
    """Create or reconcile builtin prompt resources in one successful transaction.

    A uniqueness constraint is the final concurrency guard.  If two editors
    initialize the same project concurrently, the loser rolls back its whole
    attempt and retries against the committed rows instead of leaving a
    partially initialized baseline.
    """

    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            project = db.execute(select(Project).where(Project.id == project_id).with_for_update()).scalar_one_or_none()
            if project is None:
                raise AppError.not_found()
            rows = [
                _ensure_default_preset_from_resource(
                    db,
                    project_id=project_id,
                    resource_key=resource_key,
                    activate=activate,
                    commit=False,
                )
                for resource_key, activate in _BUILTIN_PROMPT_DEFAULTS
            ]
            db.commit()
            for row in rows:
                db.refresh(row)
            return rows
        except IntegrityError as exc:
            db.rollback()
            if not _is_builtin_resource_unique_conflict(exc) or attempt + 1 >= max_attempts:
                raise
        except OperationalError as exc:
            db.rollback()
            if "locked" not in str(exc).lower() or attempt + 1 >= max_attempts:
                raise
        except Exception:
            db.rollback()
            raise
        time.sleep(0.02 * (attempt + 1))

    raise RuntimeError("unreachable builtin prompt synchronization retry state")


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
        raise AppError.validation(
            message="PromptPreset is not bound to a default resource; reset_to_default is unavailable"
        )

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
        raise AppError.validation(
            message="PromptPreset is not bound to a default resource; block reset_to_default is unavailable"
        )

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
    block.origin_template_hash = prompt_template_hash(block.template)
    block.resource_template_outdated = False

    preset.updated_at = utc_now()
    db.commit()
    db.refresh(block)
    return block


def get_active_preset_for_task(
    db: Session, *, project_id: str, task: str, allow_autocreate: bool = False
) -> PromptPreset:
    from app.services.prompt_presets import LEGACY_IMPORTED_SCOPE, parse_json_list

    presets = (
        db.execute(
            select(PromptPreset).where(PromptPreset.project_id == project_id).order_by(PromptPreset.updated_at.desc())
        )
        .scalars()
        .all()
    )

    for preset in presets:
        if (preset.scope or "") == LEGACY_IMPORTED_SCOPE:
            continue
        if task in parse_json_list(preset.active_for_json):
            return preset

    for preset in presets:
        if (preset.scope or "") != LEGACY_IMPORTED_SCOPE:
            continue
        if task in parse_json_list(preset.active_for_json):
            return preset

    raise AppError.validation(
        message=f"No PromptPreset is configured for task={task}; synchronize defaults or activate one in Prompt Studio first"
    )
