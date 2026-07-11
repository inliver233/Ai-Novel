from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.utils import new_id, utc_now
from app.models.project_default_style import ProjectDefaultStyle
from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
from app.models.writing_style import WritingStyle
from app.schemas.prompt_studio import (
    PromptStudioCategory,
    PromptStudioPresetCreate,
    PromptStudioPresetDetail,
    PromptStudioPresetSummary,
    PromptStudioPresetUpdate,
)
from app.services.prompt_preset_resources import load_preset_resource
from app.services.prompt_presets import (
    parse_json_list,
    refresh_prompt_block_resource_status,
)
from app.services.prompt_task_catalog import PROMPT_TASK_SET


@dataclass(frozen=True, slots=True)
class PromptStudioPromptCategoryConfig:
    key: str
    label: str
    task: str
    resource_key: str
    guidance_identifier: str
    output_contract_start_marker: str | None = None
    output_contract_end_marker: str | None = None
    output_wrapper_tag: str | None = None


_PROMPT_STUDIO_PROMPT_CATEGORIES: tuple[PromptStudioPromptCategoryConfig, ...] = (
    PromptStudioPromptCategoryConfig(
        key="outline_generate",
        label="大纲生成",
        task="outline_generate",
        resource_key="outline_generate_v3",
        guidance_identifier="sys.outline.role",
    ),
    PromptStudioPromptCategoryConfig(
        key="chapter_generate",
        label="章节生成",
        task="chapter_generate",
        resource_key="chapter_generate_v4",
        guidance_identifier="sys.chapter.core_role",
    ),
    PromptStudioPromptCategoryConfig(
        key="plan_chapter",
        label="章节分析",
        task="plan_chapter",
        resource_key="plan_chapter_v1",
        guidance_identifier="sys.plan_chapter.role",
        output_contract_start_marker="<OUTPUT_CONTRACT>",
        output_contract_end_marker="</OUTPUT_CONTRACT>",
        output_wrapper_tag="<plan>",
    ),
    PromptStudioPromptCategoryConfig(
        key="post_edit",
        label="章节重写",
        task="post_edit",
        resource_key="post_edit_v1",
        guidance_identifier="sys.post_edit.role",
        output_contract_start_marker="<OUTPUT_CONTRACT>",
        output_contract_end_marker="</OUTPUT_CONTRACT>",
        output_wrapper_tag="<rewrite>",
    ),
    PromptStudioPromptCategoryConfig(
        key="content_optimize",
        label="正文优化",
        task="content_optimize",
        resource_key="content_optimize_v1",
        guidance_identifier="sys.content_optimize.role",
        output_contract_start_marker="<OUTPUT_CONTRACT>",
        output_contract_end_marker="</OUTPUT_CONTRACT>",
        output_wrapper_tag="<content>",
    ),
)
_PROMPT_STUDIO_PROMPT_CATEGORY_BY_KEY = {item.key: item for item in _PROMPT_STUDIO_PROMPT_CATEGORIES}
PROMPT_STUDIO_PROMPT_TASK_SET: frozenset[str] = frozenset(
    item.task for item in _PROMPT_STUDIO_PROMPT_CATEGORIES
)
_UNKNOWN_PROMPT_TASKS = PROMPT_STUDIO_PROMPT_TASK_SET - PROMPT_TASK_SET
_MISSING_STUDIO_TASKS = PROMPT_TASK_SET - PROMPT_STUDIO_PROMPT_TASK_SET
if _UNKNOWN_PROMPT_TASKS or _MISSING_STUDIO_TASKS:
    unknown = ", ".join(sorted(_UNKNOWN_PROMPT_TASKS)) or "none"
    missing = ", ".join(sorted(_MISSING_STUDIO_TASKS)) or "none"
    raise RuntimeError(f"Prompt Studio task catalog mismatch (unknown: {unknown}; missing: {missing})")
_PROMPT_STUDIO_WRITING_STYLE_KEY = "writing_style"
_PROMPT_STUDIO_WRITING_STYLE_LABEL = "写作风格"


def _require_category_config(category: str) -> PromptStudioPromptCategoryConfig:
    config = _PROMPT_STUDIO_PROMPT_CATEGORY_BY_KEY.get(str(category or "").strip())
    if config is None:
        raise AppError.validation(message="无效的 Prompt Studio 分类（category）")
    return config


def _list_project_presets(db: Session, *, project_id: str) -> list[PromptPreset]:
    return (
        db.execute(
            select(PromptPreset)
            .where(PromptPreset.project_id == project_id)
            .order_by(PromptPreset.updated_at.desc(), PromptPreset.created_at.desc())
        )
        .scalars()
        .all()
    )


def _list_blocks_for_presets(db: Session, *, preset_ids: list[str]) -> dict[str, list[PromptBlock]]:
    grouped: dict[str, list[PromptBlock]] = defaultdict(list)
    if not preset_ids:
        return grouped
    rows = (
        db.execute(
            select(PromptBlock)
            .where(PromptBlock.preset_id.in_(preset_ids))
            .order_by(PromptBlock.injection_order.asc(), PromptBlock.created_at.asc())
        )
        .scalars()
        .all()
    )
    for row in rows:
        grouped[row.preset_id].append(row)
    return grouped


def _require_project_prompt_preset(db: Session, *, project_id: str, preset_id: str) -> PromptPreset:
    preset = db.get(PromptPreset, preset_id)
    if preset is None or preset.project_id != project_id:
        raise AppError.not_found()
    return preset


def _can_use_style(style: WritingStyle, *, user_id: str) -> bool:
    return bool(style.is_preset) or style.owner_user_id == user_id


def _require_accessible_style(db: Session, *, style_id: str, user_id: str) -> WritingStyle:
    row = db.get(WritingStyle, style_id)
    if row is None:
        raise AppError.not_found()
    if not _can_use_style(row, user_id=user_id):
        raise AppError.not_found()
    return row


def _require_mutable_style(db: Session, *, style_id: str, user_id: str) -> WritingStyle:
    row = db.get(WritingStyle, style_id)
    if row is None:
        raise AppError.not_found()
    if row.is_preset:
        raise AppError.validation(message="预置写作风格不支持编辑")
    if row.owner_user_id != user_id:
        raise AppError.not_found()
    return row


def _require_deletable_style(db: Session, *, style_id: str, user_id: str) -> WritingStyle:
    row = db.get(WritingStyle, style_id)
    if row is None:
        raise AppError.not_found()
    if row.is_preset:
        raise AppError.validation(message="预置写作风格不能删除")
    if row.owner_user_id != user_id:
        raise AppError.not_found()
    return row


def _load_guidance_template_from_resource(config: PromptStudioPromptCategoryConfig) -> str:
    resource = load_preset_resource(config.resource_key)
    block = next((item for item in resource.blocks if item.identifier == config.guidance_identifier), None)
    if block is None:
        raise AppError(
            code="PROMPT_STUDIO_RESOURCE_INVALID",
            message="Prompt Studio 默认资源缺少可编辑引导块",
            status_code=500,
            details={"resource_key": config.resource_key, "identifier": config.guidance_identifier},
        )
    return str(block.template or "").strip()


def _output_contract_bounds(config: PromptStudioPromptCategoryConfig, text: str) -> tuple[int, int] | None:
    start_marker = config.output_contract_start_marker
    end_marker = config.output_contract_end_marker
    if not start_marker or not end_marker or not config.output_wrapper_tag:
        return None
    start = text.rfind(start_marker)
    if start < 0:
        return None
    end = text.find(end_marker, start + len(start_marker))
    if end < 0:
        return None
    end += len(end_marker)
    if config.output_wrapper_tag not in text[start:end]:
        return None
    return start, end


def _output_contract_block(config: PromptStudioPromptCategoryConfig) -> str:
    if not config.output_contract_start_marker or not config.output_contract_end_marker:
        return ""
    template = _load_guidance_template_from_resource(config)
    bounds = _output_contract_bounds(config, template)
    if bounds is None:
        raise AppError(
            code="PROMPT_STUDIO_RESOURCE_INVALID",
            message="Prompt Studio 默认资源缺少结构化输出合同",
            status_code=500,
            details={
                "resource_key": config.resource_key,
                "identifier": config.guidance_identifier,
                "start_marker": config.output_contract_start_marker,
                "end_marker": config.output_contract_end_marker,
            },
        )
    start, end = bounds
    return template[start:end].strip()


def _extract_editable_content(config: PromptStudioPromptCategoryConfig, template: str | None) -> str:
    text = str(template or "").strip()
    if not text:
        return ""
    if not config.output_contract_start_marker or not config.output_contract_end_marker:
        return text

    bounds = _output_contract_bounds(config, text)
    if bounds is not None:
        start, end = bounds
        return "\n\n".join(part for part in (text[:start].strip(), text[end:].strip()) if part)
    return text


def _compose_guidance_template(config: PromptStudioPromptCategoryConfig, content: str) -> str:
    editable_content = str(content or "").strip()
    reserved_markers = (
        config.output_contract_start_marker,
        config.output_contract_end_marker,
    )
    if any(marker and marker in editable_content for marker in reserved_markers):
        raise AppError.validation(message="自定义提示词不能包含保留的输出合同标记")
    contract = _output_contract_block(config)
    if not contract:
        return editable_content
    return f"{editable_content}\n\n{contract}".strip()


def _build_prompt_block_from_resource(
    *,
    preset_id: str,
    identifier: str,
    name: str,
    role: str,
    enabled: bool,
    template: str,
    marker_key: str | None,
    injection_position: str,
    injection_depth: int | None,
    injection_order: int,
    triggers: list[str],
    forbid_overrides: bool,
    budget: dict | None,
    cache: dict | None,
) -> PromptBlock:
    return PromptBlock(
        id=new_id(),
        preset_id=preset_id,
        identifier=identifier,
        name=name,
        role=role,
        enabled=enabled,
        template=template,
        marker_key=marker_key,
        injection_position=injection_position,
        injection_depth=injection_depth,
        injection_order=injection_order,
        triggers_json=json.dumps(list(triggers or []), ensure_ascii=False),
        forbid_overrides=forbid_overrides,
        budget_json=json.dumps(budget, ensure_ascii=False) if budget else None,
        cache_json=json.dumps(cache, ensure_ascii=False) if cache else None,
    )


def _preset_matches_config(
    preset: PromptPreset,
    *,
    blocks: list[PromptBlock],
    config: PromptStudioPromptCategoryConfig,
) -> bool:
    if preset.resource_key == config.resource_key:
        return True
    if config.task in parse_json_list(preset.active_for_json):
        return True
    return any(block.identifier == config.guidance_identifier for block in blocks)


def _resolve_prompt_category_for_preset(
    preset: PromptPreset,
    *,
    blocks: list[PromptBlock],
) -> PromptStudioPromptCategoryConfig | None:
    for config in _PROMPT_STUDIO_PROMPT_CATEGORIES:
        if any(block.identifier == config.guidance_identifier for block in blocks):
            return config
    for config in _PROMPT_STUDIO_PROMPT_CATEGORIES:
        if _preset_matches_config(preset, blocks=blocks, config=config):
            return config
    return None


def _require_prompt_studio_preset(
    db: Session,
    *,
    project_id: str,
    preset_id: str,
    expected_config: PromptStudioPromptCategoryConfig | None = None,
) -> tuple[PromptPreset, list[PromptBlock], PromptStudioPromptCategoryConfig]:
    preset = _require_project_prompt_preset(db, project_id=project_id, preset_id=preset_id)
    blocks = (
        db.execute(
            select(PromptBlock)
            .where(PromptBlock.preset_id == preset.id)
            .order_by(PromptBlock.injection_order.asc(), PromptBlock.created_at.asc())
        )
        .scalars()
        .all()
    )
    resolved = _resolve_prompt_category_for_preset(preset, blocks=blocks)
    if resolved is None:
        raise AppError.validation(message="该 PromptPreset 不属于 Prompt Studio 支持的分类")
    if expected_config is not None and resolved.key != expected_config.key:
        raise AppError.validation(message=f"该 PromptPreset 不属于分类 {expected_config.key}")
    return preset, blocks, resolved


def _find_guidance_block(
    *,
    blocks: list[PromptBlock],
    config: PromptStudioPromptCategoryConfig,
) -> PromptBlock:
    row = next((block for block in blocks if block.identifier == config.guidance_identifier), None)
    if row is None:
        raise AppError.validation(message="PromptPreset 缺少 Prompt Studio 所需的可编辑引导块")
    return row


def _has_guidance_block(
    *,
    blocks: list[PromptBlock],
    config: PromptStudioPromptCategoryConfig,
) -> bool:
    return any(block.identifier == config.guidance_identifier for block in blocks)


def _prompt_detail_from_row(
    *,
    preset: PromptPreset,
    config: PromptStudioPromptCategoryConfig,
    guidance_block: PromptBlock,
) -> dict[str, object]:
    detail = PromptStudioPresetDetail(
        id=preset.id,
        name=preset.name,
        content=_extract_editable_content(config, guidance_block.template),
        is_active=config.task in parse_json_list(preset.active_for_json),
    )
    return detail.model_dump()


def _prompt_summary_from_row(
    *,
    preset: PromptPreset,
    config: PromptStudioPromptCategoryConfig,
) -> PromptStudioPresetSummary:
    return PromptStudioPresetSummary(
        id=preset.id,
        name=preset.name,
        is_active=config.task in parse_json_list(preset.active_for_json),
    )


def _style_detail_from_row(
    *,
    row: WritingStyle,
    active_style_id: str | None,
) -> dict[str, object]:
    detail = PromptStudioPresetDetail(
        id=row.id,
        name=row.name,
        content=(row.prompt_content or "").strip(),
        is_active=row.id == active_style_id,
    )
    return detail.model_dump()


def _style_summary_from_row(
    *,
    row: WritingStyle,
    active_style_id: str | None,
) -> PromptStudioPresetSummary:
    return PromptStudioPresetSummary(id=row.id, name=row.name, is_active=row.id == active_style_id)


def _list_style_rows(db: Session, *, user_id: str) -> list[WritingStyle]:
    preset_rows = (
        db.execute(select(WritingStyle).where(WritingStyle.is_preset == True).order_by(WritingStyle.name.asc()))
        .scalars()
        .all()
    )
    user_rows = (
        db.execute(
            select(WritingStyle)
            .where(WritingStyle.owner_user_id == user_id)
            .where(WritingStyle.is_preset == False)
            .order_by(WritingStyle.updated_at.desc(), WritingStyle.name.asc())
        )
        .scalars()
        .all()
    )
    return [*preset_rows, *user_rows]


def list_categories_payload(db: Session, *, project_id: str, user_id: str) -> dict[str, object]:
    presets = _list_project_presets(db, project_id=project_id)
    blocks_by_preset_id = _list_blocks_for_presets(db, preset_ids=[preset.id for preset in presets])

    categories: list[dict[str, object]] = []
    for config in _PROMPT_STUDIO_PROMPT_CATEGORIES:
        matched = [
            _prompt_summary_from_row(preset=preset, config=config)
            for preset in presets
            if _has_guidance_block(blocks=blocks_by_preset_id.get(preset.id, []), config=config)
            and _preset_matches_config(preset, blocks=blocks_by_preset_id.get(preset.id, []), config=config)
        ]
        categories.append(
            PromptStudioCategory(
                key=config.key,
                label=config.label,
                task=config.task,
                presets=[item for item in matched],
            ).model_dump()
        )

    default_style = db.get(ProjectDefaultStyle, project_id)
    active_style_id = default_style.style_id if default_style else None
    style_rows = _list_style_rows(db, user_id=user_id)
    categories.append(
        PromptStudioCategory(
            key=_PROMPT_STUDIO_WRITING_STYLE_KEY,
            label=_PROMPT_STUDIO_WRITING_STYLE_LABEL,
            task=None,
            presets=[_style_summary_from_row(row=row, active_style_id=active_style_id) for row in style_rows],
        ).model_dump()
    )

    return {"categories": categories}


def get_preset_detail_payload(
    db: Session,
    *,
    project_id: str,
    user_id: str,
    preset_id: str,
    category: str,
) -> dict[str, object]:
    if category == _PROMPT_STUDIO_WRITING_STYLE_KEY:
        default_style = db.get(ProjectDefaultStyle, project_id)
        active_style_id = default_style.style_id if default_style else None
        row = _require_accessible_style(db, style_id=preset_id, user_id=user_id)
        return {"preset": _style_detail_from_row(row=row, active_style_id=active_style_id)}

    config = _require_category_config(category)
    preset, blocks, _ = _require_prompt_studio_preset(
        db,
        project_id=project_id,
        preset_id=preset_id,
        expected_config=config,
    )
    guidance_block = _find_guidance_block(blocks=blocks, config=config)
    return {"preset": _prompt_detail_from_row(preset=preset, config=config, guidance_block=guidance_block)}


def create_preset_payload(
    db: Session,
    *,
    project_id: str,
    user_id: str,
    category: str,
    body: PromptStudioPresetCreate,
) -> dict[str, object]:
    if category == _PROMPT_STUDIO_WRITING_STYLE_KEY:
        row = WritingStyle(
            id=new_id(),
            owner_user_id=user_id,
            name=body.name,
            description=None,
            prompt_content=body.content,
            is_preset=False,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        default_style = db.get(ProjectDefaultStyle, project_id)
        active_style_id = default_style.style_id if default_style else None
        return {"preset": _style_detail_from_row(row=row, active_style_id=active_style_id)}

    config = _require_category_config(category)
    resource = load_preset_resource(config.resource_key)

    preset = PromptPreset(
        id=new_id(),
        project_id=project_id,
        name=body.name,
        resource_key=None,
        category=resource.category,
        scope=resource.scope,
        version=resource.version,
        active_for_json=json.dumps([], ensure_ascii=False),
    )
    db.add(preset)
    db.flush()

    blocks: list[PromptBlock] = []
    for block_resource in resource.blocks:
        template = str(block_resource.template or "")
        if block_resource.identifier == config.guidance_identifier:
            template = _compose_guidance_template(config, body.content)
        blocks.append(
            _build_prompt_block_from_resource(
                preset_id=preset.id,
                identifier=str(block_resource.identifier),
                name=str(block_resource.name),
                role=str(block_resource.role),
                enabled=bool(block_resource.enabled),
                template=template,
                marker_key=block_resource.marker_key,
                injection_position=str(block_resource.injection_position),
                injection_depth=block_resource.injection_depth,
                injection_order=int(block_resource.injection_order),
                triggers=list(block_resource.triggers or []),
                forbid_overrides=bool(block_resource.forbid_overrides),
                budget=block_resource.budget,
                cache=block_resource.cache,
            )
        )

    db.add_all(blocks)
    db.commit()
    db.refresh(preset)

    guidance_block = _find_guidance_block(blocks=blocks, config=config)
    return {"preset": _prompt_detail_from_row(preset=preset, config=config, guidance_block=guidance_block)}


def update_preset_payload(
    db: Session,
    *,
    project_id: str,
    user_id: str,
    preset_id: str,
    body: PromptStudioPresetUpdate,
) -> dict[str, object]:
    preset = db.get(PromptPreset, preset_id)
    if preset is not None and preset.project_id == project_id:
        prompt_preset, blocks, config = _require_prompt_studio_preset(db, project_id=project_id, preset_id=preset_id)
        guidance_block = _find_guidance_block(blocks=blocks, config=config)

        if body.name is not None:
            prompt_preset.name = body.name
        if body.content is not None:
            guidance_block.template = _compose_guidance_template(config, body.content)
            refresh_prompt_block_resource_status(guidance_block)
        prompt_preset.updated_at = utc_now()

        db.commit()
        db.refresh(prompt_preset)
        db.refresh(guidance_block)
        return {"preset": _prompt_detail_from_row(preset=prompt_preset, config=config, guidance_block=guidance_block)}

    row = _require_mutable_style(db, style_id=preset_id, user_id=user_id)
    if body.name is not None:
        row.name = body.name
    if body.content is not None:
        row.prompt_content = body.content
    db.commit()
    db.refresh(row)

    default_style = db.get(ProjectDefaultStyle, project_id)
    active_style_id = default_style.style_id if default_style else None
    return {"preset": _style_detail_from_row(row=row, active_style_id=active_style_id)}


def delete_preset_payload(
    db: Session,
    *,
    project_id: str,
    user_id: str,
    preset_id: str,
) -> dict[str, object]:
    preset = db.get(PromptPreset, preset_id)
    if preset is not None and preset.project_id == project_id:
        prompt_preset, blocks, _ = _require_prompt_studio_preset(db, project_id=project_id, preset_id=preset_id)
        for block in blocks:
            db.delete(block)
        db.flush()
        db.delete(prompt_preset)
        db.commit()
        return {}

    row = _require_deletable_style(db, style_id=preset_id, user_id=user_id)
    db.execute(update(ProjectDefaultStyle).where(ProjectDefaultStyle.style_id == preset_id).values(style_id=None))
    db.delete(row)
    db.commit()
    return {}


def activate_preset_payload(
    db: Session,
    *,
    project_id: str,
    user_id: str,
    preset_id: str,
    category: str,
) -> dict[str, object]:
    if category == _PROMPT_STUDIO_WRITING_STYLE_KEY:
        row = _require_accessible_style(db, style_id=preset_id, user_id=user_id)
        default_style = db.get(ProjectDefaultStyle, project_id)
        if default_style is None:
            default_style = ProjectDefaultStyle(project_id=project_id, style_id=row.id)
            db.add(default_style)
        else:
            default_style.style_id = row.id
        db.commit()
        db.refresh(default_style)
        return {"preset": _style_detail_from_row(row=row, active_style_id=default_style.style_id)}

    config = _require_category_config(category)
    target_preset, _, _ = _require_prompt_studio_preset(
        db,
        project_id=project_id,
        preset_id=preset_id,
        expected_config=config,
    )

    presets = _list_project_presets(db, project_id=project_id)
    blocks_by_preset_id = _list_blocks_for_presets(db, preset_ids=[preset.id for preset in presets])

    for row in presets:
        if not _preset_matches_config(row, blocks=blocks_by_preset_id.get(row.id, []), config=config):
            continue
        tasks = parse_json_list(row.active_for_json)
        next_tasks = [item for item in tasks if item != config.task]
        if row.id == target_preset.id and config.task not in next_tasks:
            next_tasks.append(config.task)
        row.active_for_json = json.dumps(next_tasks, ensure_ascii=False)

    target_preset.updated_at = utc_now()
    db.commit()

    refreshed_preset, blocks, _ = _require_prompt_studio_preset(
        db,
        project_id=project_id,
        preset_id=preset_id,
        expected_config=config,
    )
    guidance_block = _find_guidance_block(blocks=blocks, config=config)
    return {"preset": _prompt_detail_from_row(preset=refreshed_preset, config=config, guidance_block=guidance_block)}
