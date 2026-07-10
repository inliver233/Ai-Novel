from __future__ import annotations

from typing import Any

from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
from app.schemas.prompt_presets import (
    PromptBlockOut,
    PromptPresetExportOut,
    PromptPresetExportPreset,
    PromptPresetOut,
    PromptPresetResourceOut,
    PromptPreviewBlock,
    PromptPreviewOut,
)
from app.services.prompt_preset_resources import PromptPresetResource
from app.services.prompt_presets import parse_json_dict, parse_json_list


def _preset_to_out(row: PromptPreset) -> dict[str, object]:
    return PromptPresetOut(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        resource_key=row.resource_key,
        category=row.category,
        scope=row.scope,
        version=row.version,
        active_for=parse_json_list(row.active_for_json),
        created_at=row.created_at,
        updated_at=row.updated_at,
    ).model_dump()


def _block_to_out(row: PromptBlock) -> dict[str, object]:
    return PromptBlockOut(
        id=row.id,
        preset_id=row.preset_id,
        identifier=row.identifier,
        name=row.name,
        role=row.role,
        enabled=row.enabled,
        template=row.template,
        marker_key=row.marker_key,
        injection_position=row.injection_position,
        injection_depth=row.injection_depth,
        injection_order=row.injection_order,
        triggers=parse_json_list(row.triggers_json),
        forbid_overrides=row.forbid_overrides,
        budget=parse_json_dict(row.budget_json),
        cache=parse_json_dict(row.cache_json),
        resource_template_outdated=row.resource_template_outdated,
        created_at=row.created_at,
        updated_at=row.updated_at,
    ).model_dump()


def _resource_to_out(resource: PromptPresetResource, preset: PromptPreset | None) -> dict[str, object]:
    return PromptPresetResourceOut(
        key=resource.key,
        name=resource.name,
        category=resource.category,
        scope=resource.scope,
        version=resource.version,
        activation_tasks=list(resource.activation_tasks or []),
        preset_id=(preset.id if preset is not None else None),
        preset_version=(preset.version if preset is not None else None),
        preset_updated_at=(preset.updated_at if preset is not None else None),
    ).model_dump()


def _build_prompt_preset_export_model(
    preset: PromptPreset,
    blocks: list[PromptBlock],
) -> PromptPresetExportOut:
    return PromptPresetExportOut(
        preset=PromptPresetExportPreset(
            name=preset.name,
            category=preset.category,
            scope=preset.scope,
            version=preset.version,
            active_for=parse_json_list(preset.active_for_json),
        ),
        blocks=[
            {
                "identifier": block.identifier,
                "name": block.name,
                "role": block.role,
                "enabled": block.enabled,
                "template": block.template,
                "marker_key": block.marker_key,
                "injection_position": block.injection_position,
                "injection_depth": block.injection_depth,
                "injection_order": block.injection_order,
                "triggers": parse_json_list(block.triggers_json),
                "forbid_overrides": block.forbid_overrides,
                "budget": parse_json_dict(block.budget_json),
                "cache": parse_json_dict(block.cache_json),
            }
            for block in blocks
        ],
    )


def _build_prompt_preview_payload(
    *,
    preset_id: str,
    task: str,
    system: str,
    user: str,
    missing: list[str],
    blocks: list[Any],
    render_log: dict[str, Any],
) -> dict[str, object]:
    return PromptPreviewOut(
        preset_id=preset_id,
        task=task,
        system=system,
        user=user,
        prompt_tokens_estimate=int(render_log.get("prompt_tokens_estimate") or 0),
        prompt_budget_tokens=(
            int(render_log["prompt_budget_tokens"])
            if isinstance(render_log.get("prompt_budget_tokens"), int)
            else None
        ),
        missing=missing,
        blocks=[
            PromptPreviewBlock(
                id=block.id,
                identifier=block.identifier,
                role=block.role,
                enabled=block.enabled,
                text=block.text,
                missing=block.missing,
                token_estimate=block.token_estimate,
            )
            for block in blocks
        ],
    ).model_dump()
