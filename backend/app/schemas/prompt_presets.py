from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.schemas.limits import MAX_JSON_CHARS_MEDIUM, MAX_JSON_CHARS_SMALL, MAX_TEMPLATE_CHARS, validate_json_chars


class PromptPresetOut(BaseModel):
    id: str
    project_id: str
    name: str
    resource_key: str | None = None
    category: str | None = None
    scope: str
    version: int
    active_for: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PromptPresetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    category: str | None = Field(default=None, max_length=64)
    scope: str = Field(default="project", min_length=1, max_length=32)
    version: int = Field(default=1, ge=1)
    active_for: list[str] = Field(default_factory=list, max_length=50)


class PromptPresetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    category: str | None = Field(default=None, max_length=64)
    scope: str | None = Field(default=None, min_length=1, max_length=32)
    version: int | None = Field(default=None, ge=1)
    active_for: list[str] | None = Field(default=None, max_length=50)


class PromptPresetResourceOut(BaseModel):
    key: str
    name: str
    category: str | None = None
    scope: str
    version: int
    activation_tasks: list[str] = Field(default_factory=list)
    preset_id: str | None = None
    preset_version: int | None = None
    preset_updated_at: datetime | None = None



class PromptBlockOut(BaseModel):
    id: str
    preset_id: str
    identifier: str
    name: str
    role: str
    enabled: bool
    template: str | None = None
    marker_key: str | None = None
    injection_position: str
    injection_depth: int | None = None
    injection_order: int
    triggers: list[str] = Field(default_factory=list)
    forbid_overrides: bool = False
    budget: dict[str, Any] = Field(default_factory=dict)
    cache: dict[str, Any] = Field(default_factory=dict)
    resource_template_outdated: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PromptBlockCreate(BaseModel):
    identifier: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    role: str = Field(min_length=1, max_length=16)
    enabled: bool = True
    template: str | None = Field(default=None, max_length=MAX_TEMPLATE_CHARS)
    marker_key: str | None = Field(default=None, max_length=255)
    injection_position: str = Field(default="relative", min_length=1, max_length=16)
    injection_depth: int | None = None
    injection_order: int = 0
    triggers: list[str] = Field(default_factory=list, max_length=50)
    forbid_overrides: bool = False
    budget: dict[str, Any] = Field(default_factory=dict, max_length=100)
    cache: dict[str, Any] = Field(default_factory=dict, max_length=100)

    @field_validator("budget")
    @classmethod
    def _validate_budget(cls, v: dict[str, Any]) -> dict[str, Any]:
        return validate_json_chars(v, max_chars=MAX_JSON_CHARS_SMALL, field_name="budget") or {}

    @field_validator("cache")
    @classmethod
    def _validate_cache(cls, v: dict[str, Any]) -> dict[str, Any]:
        return validate_json_chars(v, max_chars=MAX_JSON_CHARS_SMALL, field_name="cache") or {}


class PromptBlockUpdate(BaseModel):
    identifier: str | None = Field(default=None, min_length=1, max_length=128)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    role: str | None = Field(default=None, min_length=1, max_length=16)
    enabled: bool | None = None
    template: str | None = Field(default=None, max_length=MAX_TEMPLATE_CHARS)
    marker_key: str | None = Field(default=None, max_length=255)
    injection_position: str | None = Field(default=None, min_length=1, max_length=16)
    injection_depth: int | None = None
    injection_order: int | None = None
    triggers: list[str] | None = Field(default=None, max_length=50)
    forbid_overrides: bool | None = None
    budget: dict[str, Any] | None = Field(default=None, max_length=100)
    cache: dict[str, Any] | None = Field(default=None, max_length=100)

    @field_validator("budget")
    @classmethod
    def _validate_budget(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        return validate_json_chars(v, max_chars=MAX_JSON_CHARS_SMALL, field_name="budget")

    @field_validator("cache")
    @classmethod
    def _validate_cache(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        return validate_json_chars(v, max_chars=MAX_JSON_CHARS_SMALL, field_name="cache")


class PromptBlockReorderRequest(BaseModel):
    ordered_block_ids: list[str] = Field(min_length=1, max_length=200)

    @field_validator("ordered_block_ids")
    @classmethod
    def _validate_ordered_block_ids(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for item in v or []:
            if not isinstance(item, str):
                raise ValueError("ordered_block_ids must be strings")
            item = item.strip()
            if not item:
                raise ValueError("ordered_block_ids cannot contain empty strings")
            if len(item) > 36:
                raise ValueError("ordered_block_id too long")
            out.append(item)
        return out


class PromptPreviewRequest(BaseModel):
    task: str = Field(min_length=1, max_length=64)
    preset_id: str | None = Field(default=None, max_length=36)
    values: dict[str, Any] = Field(default_factory=dict, max_length=200)

    @field_validator("values")
    @classmethod
    def _validate_values(cls, v: dict[str, Any]) -> dict[str, Any]:
        return validate_json_chars(v, max_chars=MAX_JSON_CHARS_MEDIUM, field_name="values") or {}


class PromptPreviewBlock(BaseModel):
    id: str
    identifier: str
    role: str
    enabled: bool
    text: str
    missing: list[str] = Field(default_factory=list)
    token_estimate: int = 0


class PromptPreviewOut(BaseModel):
    preset_id: str
    task: str
    system: str
    user: str
    prompt_tokens_estimate: int = 0
    prompt_budget_tokens: int | None = None
    missing: list[str] = Field(default_factory=list)
    blocks: list[PromptPreviewBlock] = Field(default_factory=list)


class PromptPresetExportBlock(BaseModel):
    identifier: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    role: str = Field(min_length=1, max_length=16)
    enabled: bool = True
    template: str | None = Field(default=None, max_length=MAX_TEMPLATE_CHARS)
    marker_key: str | None = Field(default=None, max_length=255)
    injection_position: str = Field(default="relative", min_length=1, max_length=16)
    injection_depth: int | None = None
    injection_order: int = 0
    triggers: list[str] = Field(default_factory=list, max_length=50)
    forbid_overrides: bool = False
    budget: dict[str, Any] = Field(default_factory=dict, max_length=100)
    cache: dict[str, Any] = Field(default_factory=dict, max_length=100)

    @field_validator("budget")
    @classmethod
    def _validate_budget(cls, v: dict[str, Any]) -> dict[str, Any]:
        return validate_json_chars(v, max_chars=MAX_JSON_CHARS_SMALL, field_name="budget") or {}

    @field_validator("cache")
    @classmethod
    def _validate_cache(cls, v: dict[str, Any]) -> dict[str, Any]:
        return validate_json_chars(v, max_chars=MAX_JSON_CHARS_SMALL, field_name="cache") or {}


class PromptPresetExportPreset(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    category: str | None = Field(default=None, max_length=64)
    scope: str = Field(default="project", min_length=1, max_length=32)
    version: int = Field(default=1, ge=1)
    active_for: list[str] = Field(default_factory=list, max_length=50)


class PromptPresetExportOut(BaseModel):
    preset: PromptPresetExportPreset
    blocks: list[PromptPresetExportBlock] = Field(default_factory=list)


class PromptPresetImportRequest(BaseModel):
    preset: PromptPresetExportPreset
    blocks: list[PromptPresetExportBlock] = Field(default_factory=list, max_length=200)


class PromptPresetExportAllOut(BaseModel):
    schema_version: str = Field(default="prompt_presets_export_all_v1", max_length=64)
    presets: list[PromptPresetExportOut] = Field(default_factory=list, max_length=200)


class PromptPresetImportAllRequest(BaseModel):
    schema_version: str = Field(default="prompt_presets_export_all_v1", max_length=64)
    dry_run: bool = False
    presets: list[PromptPresetExportOut] = Field(default_factory=list, max_length=200)
