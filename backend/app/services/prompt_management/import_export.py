from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.prompt_management.crud import (
    _build_prompt_block_row,
    _build_prompt_preset_row,
    _list_prompt_block_rows,
    _list_prompt_preset_rows,
)
from app.services.prompt_management.mappers import _build_prompt_preset_export_model, _preset_to_out
from app.services.prompt_management.models import _PromptImportAllState
from app.core.errors import AppError
from app.db.utils import utc_now
from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
from app.schemas.prompt_presets import PromptPresetExportAllOut, PromptPresetImportAllRequest, PromptPresetImportRequest


def _build_prompt_preset_export_payload(
    db: Session,
    *,
    preset: PromptPreset,
) -> dict[str, object]:
    blocks = _list_prompt_block_rows(db, preset_id=preset.id)
    return {"export": _build_prompt_preset_export_model(preset, blocks).model_dump()}


def _import_prompt_preset_payload(
    db: Session,
    *,
    project_id: str,
    body: PromptPresetImportRequest,
) -> dict[str, object]:
    preset = _build_prompt_preset_row(project_id=project_id, preset=body.preset)
    db.add(preset)
    db.flush()
    for block in body.blocks:
        db.add(_build_prompt_block_row(preset_id=preset.id, block=block))
    db.commit()
    db.refresh(preset)
    return {"preset": _preset_to_out(preset)}


def _build_prompt_presets_export_all_payload(
    db: Session,
    *,
    project_id: str,
) -> dict[str, object]:
    export_presets = []
    for preset in _list_prompt_preset_rows(db, project_id=project_id):
        blocks = _list_prompt_block_rows(db, preset_id=preset.id)
        export_presets.append(_build_prompt_preset_export_model(preset, blocks))
    return {"export": PromptPresetExportAllOut(presets=export_presets).model_dump()}


def _apply_prompt_import_all_item(
    db: Session,
    *,
    project_id: str,
    dry_run: bool,
    item: object,
    matches: list[PromptPreset],
    state: _PromptImportAllState,
) -> None:
    key = (str(getattr(item.preset, "name", "") or "").strip(), str(getattr(item.preset, "scope", "") or "").strip())
    if len(matches) > 1:
        state.skipped += 1
        state.conflicts.append(
            {"name": key[0], "scope": key[1], "reason": "multiple_existing", "existing_count": len(matches)}
        )
        state.actions.append({"name": key[0], "scope": key[1], "action": "skip", "reason": "multiple_existing"})
        return

    if not matches:
        state.created += 1
        state.actions.append({"name": key[0], "scope": key[1], "action": "create", "blocks": len(item.blocks)})
        if dry_run:
            return
        preset = _build_prompt_preset_row(project_id=project_id, preset=item.preset)
        db.add(preset)
        db.flush()
    else:
        state.updated += 1
        preset = matches[0]
        state.actions.append(
            {"name": key[0], "scope": key[1], "action": "update", "preset_id": preset.id, "blocks": len(item.blocks)}
        )
        if dry_run:
            return

        preset.category = item.preset.category
        preset.version = item.preset.version
        preset.active_for_json = json.dumps(item.preset.active_for or [], ensure_ascii=False)
        preset.updated_at = utc_now()

        existing_blocks = db.execute(select(PromptBlock).where(PromptBlock.preset_id == preset.id)).scalars().all()
        for block in existing_blocks:
            db.delete(block)
        db.flush()

    ordered_blocks = sorted(
        list(item.blocks or []),
        key=lambda block: (int(block.injection_order or 0), str(block.identifier or "")),
    )
    for idx, block in enumerate(ordered_blocks):
        db.add(_build_prompt_block_row(preset_id=preset.id, block=block, default_injection_order=idx))


def _build_prompt_import_all_payload(
    db: Session,
    *,
    project_id: str,
    body: PromptPresetImportAllRequest,
) -> dict[str, object]:
    if str(body.schema_version or "").strip() != "prompt_presets_export_all_v1":
        raise AppError.validation(
            details={"reason": "unsupported_schema_version", "schema_version": body.schema_version}
        )

    existing = _list_prompt_preset_rows(db, project_id=project_id)
    by_key: dict[tuple[str, str], list[PromptPreset]] = {}
    for row in existing:
        key = (str(row.name or "").strip(), str(row.scope or "").strip())
        by_key.setdefault(key, []).append(row)

    state = _PromptImportAllState()
    for item in body.presets:
        key = (str(item.preset.name or "").strip(), str(item.preset.scope or "").strip())
        _apply_prompt_import_all_item(
            db,
            project_id=project_id,
            dry_run=bool(body.dry_run),
            item=item,
            matches=by_key.get(key) or [],
            state=state,
        )

    if not body.dry_run:
        db.commit()

    return {
        "dry_run": bool(body.dry_run),
        "created": int(state.created),
        "updated": int(state.updated),
        "skipped": int(state.skipped),
        "conflicts": state.conflicts,
        "actions": state.actions,
    }
