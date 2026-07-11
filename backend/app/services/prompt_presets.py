from __future__ import annotations

"""Thin re-export hub for prompt presets.

Keep importing from `app.services.prompt_presets` to avoid churn in callers.
Implementation is split into focused modules:
- `prompt_preset_defaults.py`: default preset management + resource resets
- `prompt_preset_render.py`: rendering engine
"""

import hashlib
import json
import time
from collections import OrderedDict
from threading import Lock
from typing import Any


LEGACY_IMPORTED_SCOPE = "legacy_imported"
DEFAULT_PLAN_PRESET_NAME = "Default plan_chapter v1"
DEFAULT_POST_EDIT_PRESET_NAME = "Default post_edit v1"
DEFAULT_CONTENT_OPTIMIZE_PRESET_NAME = "Default content_optimize v1"
DEFAULT_OUTLINE_PRESET_NAME = "榛樿路澶х翰鐢熸垚 v3锛堟帹鑽愶級"
DEFAULT_CHAPTER_PRESET_NAME = "榛樿路绔犺妭鐢熸垚 v3锛堟帹鑽愶級"
DEFAULT_CHAPTER_ANALYZE_PRESET_NAME = "榛樿路绔犺妭鍒嗘瀽 v1锛堟帹鑽愶級"
DEFAULT_CHAPTER_REWRITE_PRESET_NAME = "榛樿路绔犺妭閲嶅啓 v1锛堟帹鑽愶級"

_PROMPT_BLOCK_RENDER_CACHE_MAX_ENTRIES = 512
_prompt_block_render_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
_prompt_block_render_cache_lock = Lock()


def _hash_json(value: Any) -> str | None:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _prompt_block_cache_get(key: str, *, ttl_seconds: int | None) -> tuple[dict[str, Any] | None, str]:
    now = time.time()
    with _prompt_block_render_cache_lock:
        entry = _prompt_block_render_cache.get(key)
        if entry is None:
            return None, "miss"
        created_at, payload = entry
        if isinstance(ttl_seconds, int) and ttl_seconds > 0 and now - created_at > ttl_seconds:
            del _prompt_block_render_cache[key]
            return None, "expired"
        _prompt_block_render_cache.move_to_end(key, last=True)
        return payload, "hit"


def _prompt_block_cache_set(key: str, *, payload: dict[str, Any]) -> None:
    now = time.time()
    with _prompt_block_render_cache_lock:
        _prompt_block_render_cache[key] = (now, payload)
        _prompt_block_render_cache.move_to_end(key, last=True)
        while len(_prompt_block_render_cache) > _PROMPT_BLOCK_RENDER_CACHE_MAX_ENTRIES:
            _prompt_block_render_cache.popitem(last=False)


def parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item:
            out.append(item)
    return out


def parse_json_dict(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    if isinstance(value, dict):
        return value
    return {}


from app.services.prompt_preset_defaults import (  # noqa: E402
    _ensure_default_preset_from_resource,
    _prompt_block_from_resource,
    ensure_default_chapter_preset,
    ensure_default_content_optimize_preset,
    ensure_default_detailed_outline_preset,
    ensure_default_outline_preset,
    ensure_default_plan_preset,
    ensure_default_post_edit_preset,
    get_active_preset_for_task,
    mark_prompt_block_resource_outdated,
    prompt_template_hash,
    refresh_prompt_block_resource_status,
    reset_prompt_block_to_default_resource,
    reset_prompt_preset_to_default_resource,
    resolve_resource_key_for_preset,
    sync_builtin_prompt_defaults,
)
from app.services.prompt_preset_render import RenderedBlock, render_preset_for_task  # noqa: E402

__all__ = [
    "LEGACY_IMPORTED_SCOPE",
    "DEFAULT_PLAN_PRESET_NAME",
    "DEFAULT_POST_EDIT_PRESET_NAME",
    "DEFAULT_CONTENT_OPTIMIZE_PRESET_NAME",
    "DEFAULT_OUTLINE_PRESET_NAME",
    "DEFAULT_CHAPTER_PRESET_NAME",
    "DEFAULT_CHAPTER_ANALYZE_PRESET_NAME",
    "DEFAULT_CHAPTER_REWRITE_PRESET_NAME",
    "_PROMPT_BLOCK_RENDER_CACHE_MAX_ENTRIES",
    "_prompt_block_render_cache",
    "_prompt_block_render_cache_lock",
    "_hash_json",
    "_hash_text",
    "_prompt_block_cache_get",
    "_prompt_block_cache_set",
    "parse_json_list",
    "parse_json_dict",
    "_prompt_block_from_resource",
    "_ensure_default_preset_from_resource",
    "ensure_default_plan_preset",
    "ensure_default_post_edit_preset",
    "ensure_default_content_optimize_preset",
    "ensure_default_detailed_outline_preset",
    "ensure_default_outline_preset",
    "ensure_default_chapter_preset",
    "resolve_resource_key_for_preset",
    "reset_prompt_preset_to_default_resource",
    "reset_prompt_block_to_default_resource",
    "get_active_preset_for_task",
    "mark_prompt_block_resource_outdated",
    "prompt_template_hash",
    "refresh_prompt_block_resource_status",
    "sync_builtin_prompt_defaults",
    "RenderedBlock",
    "render_preset_for_task",
]
