from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.capabilities import max_context_tokens_limit, max_output_tokens_limit
from app.llm.messages import ChatMessage, flatten_messages, normalize_role
from app.models.llm_preset import LLMPreset
from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
from app.services.context_optimizer import ContextOptimizer
from app.services.prompt_budget import estimate_tokens, trim_text_to_tokens
from app.services.prompting import render_template

from app.services.prompt_preset_defaults import get_active_preset_for_task


@dataclass(slots=True)
class RenderedBlock:
    id: str
    identifier: str
    role: str
    enabled: bool
    text: str
    missing: list[str]
    token_estimate: int


def render_preset_for_task(
    db: Session,
    *,
    project_id: str,
    task: str,
    values: dict[str, Any],
    preset_id: str | None = None,
    macro_seed: str | None = None,
    provider: str | None = None,
    prompt_budget_tokens: int | None = None,
    allow_autocreate: bool = False,
) -> tuple[str, str, list[ChatMessage], list[str], list[RenderedBlock], str, dict]:
    from app.services.prompt_presets import (
        _hash_json,
        _hash_text,
        _prompt_block_cache_get,
        _prompt_block_cache_set,
        parse_json_dict,
        parse_json_list,
    )

    if preset_id is None:
        preset = get_active_preset_for_task(db, project_id=project_id, task=task, allow_autocreate=allow_autocreate)
    else:
        preset = db.get(PromptPreset, preset_id)
        if preset is None or preset.project_id != project_id:
            preset = get_active_preset_for_task(db, project_id=project_id, task=task, allow_autocreate=allow_autocreate)

    blocks = (
        db.execute(
            select(PromptBlock)
            .where(PromptBlock.preset_id == preset.id)
            .order_by(PromptBlock.injection_order.asc(), PromptBlock.created_at.asc())
        )
        .scalars()
        .all()
    )

    priority_rank: dict[str, int] = {"drop_first": 0, "optional": 1, "important": 2, "must": 3}
    default_budget_by_provider: dict[str, int] = {
        "openai": 24000,
        "openai_responses": 24000,
        "openai_compatible": 24000,
        "openai_responses_compatible": 24000,
        "anthropic": 12000,
        "gemini": 12000,
    }
    budget_tokens = prompt_budget_tokens
    budget_source = "explicit" if budget_tokens is not None else "unset"
    budget_calc: dict[str, Any] | None = None
    if budget_tokens is None:
        llm_preset = db.get(LLMPreset, project_id)
        effective_provider = str(provider or (llm_preset.provider if llm_preset is not None else "")).strip()
        effective_model = (
            str(llm_preset.model or "").strip()
            if llm_preset is not None and (provider is None or provider == llm_preset.provider)
            else None
        )

        max_ctx = max_context_tokens_limit(effective_provider, effective_model)
        max_out = max_output_tokens_limit(effective_provider, effective_model)
        safety_margin = 512

        if isinstance(max_ctx, int) and max_ctx > 0 and isinstance(max_out, int) and max_out > 0:
            computed = int(max_ctx) - int(max_out) - int(safety_margin)
            if computed > 0:
                budget_tokens = computed
                budget_source = "capabilities"
            else:
                budget_tokens = default_budget_by_provider.get(effective_provider or "", 24000)
                budget_source = "provider_default"
            budget_calc = {
                "provider": effective_provider,
                "model": effective_model,
                "max_context_tokens": int(max_ctx),
                "max_output_tokens": int(max_out),
                "safety_margin_tokens": int(safety_margin),
                "computed_budget_tokens": int(computed),
            }
        else:
            budget_tokens = default_budget_by_provider.get(effective_provider or "", 24000)
            budget_source = "provider_default"
            budget_calc = {
                "provider": effective_provider,
                "model": effective_model,
                "max_context_tokens": int(max_ctx) if isinstance(max_ctx, int) else None,
                "max_output_tokens": int(max_out) if isinstance(max_out, int) else None,
                "safety_margin_tokens": int(safety_margin),
                "computed_budget_tokens": None,
            }

    all_missing: set[str] = set()
    block_states: list[dict] = []
    effective_index_by_identifier: dict[str, int] = {}
    cache_hit: list[dict[str, Any]] = []
    cache_miss: list[dict[str, Any]] = []

    def _try_get_marker_value(values_obj: dict[str, Any], marker_key: str) -> tuple[bool, Any]:
        if marker_key in values_obj:
            return True, values_obj.get(marker_key)
        if "." not in marker_key:
            return False, None
        cur: Any = values_obj
        for part in marker_key.split("."):
            if isinstance(cur, dict):
                if part not in cur:
                    return False, None
                cur = cur.get(part)
                continue
            if isinstance(cur, list) and part.isdigit():
                idx = int(part)
                if idx < 0 or idx >= len(cur):
                    return False, None
                cur = cur[idx]
                continue
            return False, None
        return True, cur

    for b in blocks:
        if not b.enabled:
            continue
        triggers = parse_json_list(b.triggers_json)
        if triggers and task not in triggers:
            continue

        text = ""
        missing: list[str] = []
        render_error: str | None = None
        reason_parts: list[str] = []

        prev_idx = effective_index_by_identifier.get(b.identifier)
        prev_state = block_states[prev_idx] if prev_idx is not None and prev_idx < len(block_states) else None
        if prev_state is not None and bool(prev_state.get("forbid_overrides")):
            reason_parts.append("override_forbidden")
        else:
            render_values = values
            base_text: str | None = None
            if prev_state is not None:
                original_text = str(prev_state.get("text_after") or prev_state.get("text_before") or "")
                base_text = original_text
                render_values = dict(values)
                render_values["original"] = original_text
                render_values["base"] = original_text

            cache_cfg = parse_json_dict(b.cache_json)
            cache_enabled = bool(cache_cfg.get("enabled", False))
            cache_ttl_seconds = cache_cfg.get("ttl_seconds", cache_cfg.get("ttl", cache_cfg.get("max_age_seconds")))
            ttl_seconds: int | None = cache_ttl_seconds if isinstance(cache_ttl_seconds, int) and cache_ttl_seconds > 0 else None

            if b.template:
                cache_key: str | None = None
                cache_status: str | None = None
                cache_reason: str | None = None
                if cache_enabled:
                    strategy = str(cache_cfg.get("key_strategy", cache_cfg.get("strategy") or "")).strip().lower() or "marker_or_values"
                    marker_key_for_cache = cache_cfg.get("marker_key")
                    marker_key = (
                        str(marker_key_for_cache).strip()
                        if isinstance(marker_key_for_cache, str) and str(marker_key_for_cache).strip()
                        else (str(b.marker_key).strip() if b.marker_key else None)
                    )

                    values_hash: str | None
                    if marker_key is not None and strategy in ("marker", "marker_key", "marker_or_values"):
                        found, marker_value = _try_get_marker_value(values, marker_key)
                        marker_hash = _hash_json(marker_value) if found else "missing"
                        values_hash = marker_hash if marker_hash is not None else None
                    else:
                        values_hash = _hash_json(values)

                    base_hash = _hash_text(base_text) if isinstance(base_text, str) else None
                    template_hash = _hash_text(str(b.template or ""))
                    seed_hash = _hash_text(str(macro_seed or ""))

                    if values_hash is None:
                        cache_status = "skip"
                        cache_reason = "unhashable_values"
                    else:
                        cache_key = f"v1|{b.id}|{task}|{template_hash}|{seed_hash}|{values_hash}|{base_hash or '-'}"
                        cached, cache_status = _prompt_block_cache_get(cache_key, ttl_seconds=ttl_seconds)
                        if cached is not None:
                            text = str(cached.get("text") or "")
                            missing = list(cached.get("missing") or [])
                            render_error = str(cached.get("render_error") or "") or None
                        else:
                            cache_reason = cache_status

                if cache_key is not None and cache_status == "hit":
                    cache_hit.append({"id": b.id, "identifier": b.identifier})
                elif cache_enabled:
                    cache_miss.append(
                        {
                            "id": b.id,
                            "identifier": b.identifier,
                            "reason": cache_reason or cache_status or "miss",
                        }
                    )

                if cache_key is None or cache_status != "hit":
                    text, missing, render_error = render_template(b.template, render_values, macro_seed=macro_seed)
                    if cache_key is not None and (render_error is None):
                        _prompt_block_cache_set(cache_key, payload={"text": text, "missing": missing, "render_error": render_error})
                if render_error:
                    reason_parts.append("template_error")
            elif b.marker_key:
                found, marker_value = _try_get_marker_value(values, b.marker_key)
                if found:
                    text = "" if marker_value is None else str(marker_value)
                else:
                    missing = [b.marker_key]
                    text = ""

        all_missing.update(missing)

        budget = parse_json_dict(b.budget_json)
        priority = str(budget.get("priority") or "important").strip().lower()
        if priority not in priority_rank:
            priority = "important"
        max_tokens = budget.get("maxTokens", budget.get("max_tokens"))
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            max_tokens = None

        tokens_before = estimate_tokens(text)
        text_after = text
        trimmed = False
        if max_tokens is not None and tokens_before > max_tokens:
            text_after = trim_text_to_tokens(text_after, max_tokens)
            trimmed = True
            reason_parts.append(f"block_max_tokens:{max_tokens}")
        tokens_after = estimate_tokens(text_after)

        block_states.append(
            {
                "id": b.id,
                "identifier": b.identifier,
                "role": b.role,
                "enabled": b.enabled,
                "missing": missing,
                "render_error": render_error,
                "priority": priority,
                "max_tokens": max_tokens,
                "injection_position": str(b.injection_position or "relative"),
                "injection_depth": (int(b.injection_depth) if b.injection_depth is not None else None),
                "order": int(b.injection_order or 0),
                "text_before": text,
                "tokens_before": tokens_before,
                "text_after": text_after,
                "tokens_after": tokens_after,
                "trimmed": trimmed,
                "dropped": False,
                "reason": ";".join(reason_parts) if reason_parts else None,
                "forbid_overrides": bool(b.forbid_overrides),
            }
        )

        # Handle overrides: later blocks with the same identifier supersede earlier ones.
        if prev_state is not None:
            if bool(prev_state.get("forbid_overrides")):
                # Keep the previous effective block; drop this one.
                block_states[-1]["text_after"] = ""
                block_states[-1]["tokens_after"] = 0
                block_states[-1]["dropped"] = True
                block_states[-1]["reason"] = (str(block_states[-1].get("reason")) + ";" if block_states[-1].get("reason") else "") + "override_forbidden"
                continue

            prev_state["text_after"] = ""
            prev_state["tokens_after"] = 0
            prev_state["dropped"] = True
            prev_state["reason"] = (str(prev_state.get("reason")) + ";" if prev_state.get("reason") else "") + "overridden"

        effective_index_by_identifier[b.identifier] = len(block_states) - 1

    optimizer_enabled = bool(values.get("context_optimizer_enabled", False))
    optimizer_log = ContextOptimizer(enabled=optimizer_enabled).optimize_prompt_block_states(block_states)

    def _context_group(identifier: str) -> str | None:
        if identifier.startswith("sys.story.smart_context."):
            return "smart_context"
        if identifier.startswith("sys.memory."):
            return "memory_pack"
        return None

    def _sum_context_tokens() -> dict[str, int]:
        smart = 0
        memory = 0
        for s in block_states:
            identifier = str(s.get("identifier") or "")
            tokens = int(s.get("tokens_after") or 0)
            if tokens <= 0:
                continue
            group = _context_group(identifier)
            if group == "smart_context":
                smart += tokens
            elif group == "memory_pack":
                memory += tokens
        return {"smart_context": int(smart), "memory_pack": int(memory), "total": int(smart + memory)}

    unified_cfg = values.get("unified_context_budget")
    unified_enabled = False
    unified_budget_tokens: int | None = None
    unified_budget_source = "disabled"
    if isinstance(unified_cfg, dict):
        unified_enabled = bool(unified_cfg.get("enabled"))
        raw_tokens = unified_cfg.get("budget_tokens", unified_cfg.get("total_tokens"))
        if isinstance(raw_tokens, int) and raw_tokens > 0:
            unified_budget_tokens = int(raw_tokens)
            unified_budget_source = "explicit"
        else:
            ratio_raw = unified_cfg.get("ratio")
            try:
                ratio = float(ratio_raw)  # type: ignore[arg-type]
            except Exception:
                ratio = 0.0
            if unified_enabled and ratio > 0 and ratio <= 1 and isinstance(budget_tokens, int) and budget_tokens > 0:
                unified_budget_tokens = max(0, int(int(budget_tokens) * ratio))
                if unified_budget_tokens > 0:
                    unified_budget_source = "ratio"

    unified_log: dict[str, Any] = {
        "enabled": bool(unified_enabled and isinstance(unified_budget_tokens, int) and unified_budget_tokens > 0),
        "budget_tokens": unified_budget_tokens,
        "budget_source": unified_budget_source,
        "before": _sum_context_tokens(),
        "after": None,
        "applied": False,
        "dropped_blocks": 0,
        "trimmed_blocks": 0,
    }

    if unified_log["enabled"] and isinstance(unified_budget_tokens, int) and unified_budget_tokens > 0:
        before_total = int((unified_log.get("before") or {}).get("total") or 0)
        if before_total > unified_budget_tokens:
            ctx_total = before_total
            dropped = 0
            trimmed = 0

            candidates = [
                s
                for s in block_states
                if _context_group(str(s.get("identifier") or "")) is not None
                and int(s.get("tokens_after") or 0) > 0
                and str(s.get("text_after") or "").strip()
            ]

            drop_candidates = [s for s in candidates if str(s.get("priority") or "") in ("drop_first", "optional", "important")]
            drop_candidates.sort(key=lambda s: (priority_rank.get(str(s.get("priority") or ""), 2), -int(s.get("order") or 0)))
            for s in drop_candidates:
                if ctx_total <= unified_budget_tokens:
                    break
                current = int(s.get("tokens_after") or 0)
                if current <= 0:
                    continue
                ctx_total -= current
                s["text_after"] = ""
                s["tokens_after"] = 0
                s["dropped"] = True
                s["reason"] = (str(s.get("reason") or "") + ";" if s.get("reason") else "") + "dropped_for_unified_context_budget"
                dropped += 1

            if ctx_total > unified_budget_tokens:
                trim_candidates = [
                    s for s in candidates if int(s.get("tokens_after") or 0) > 0 and str(s.get("text_after") or "").strip()
                ]
                trim_candidates.sort(
                    key=lambda s: (priority_rank.get(str(s.get("priority") or ""), 2), -int(s.get("order") or 0))
                )
                for s in trim_candidates:
                    if ctx_total <= unified_budget_tokens:
                        break
                    current = int(s.get("tokens_after") or 0)
                    if current <= 0:
                        continue
                    need = ctx_total - unified_budget_tokens
                    target = max(0, current - need)
                    trimmed_text = trim_text_to_tokens(str(s.get("text_after") or ""), target)
                    new_tokens = estimate_tokens(trimmed_text)
                    if new_tokens >= current:
                        continue
                    ctx_total -= current - new_tokens
                    s["text_after"] = trimmed_text
                    s["tokens_after"] = new_tokens
                    s["trimmed"] = True
                    s["reason"] = (str(s.get("reason") or "") + ";" if s.get("reason") else "") + f"trim_for_unified_context_budget:{target}"
                    trimmed += 1

            unified_log["applied"] = True
            unified_log["dropped_blocks"] = int(dropped)
            unified_log["trimmed_blocks"] = int(trimmed)

    unified_log["after"] = _sum_context_tokens()

    total_tokens = sum(int(s["tokens_after"]) for s in block_states)
    if budget_tokens is not None and total_tokens > budget_tokens:
        candidates = [s for s in block_states if s["priority"] in ("drop_first", "optional", "important")]
        candidates.sort(key=lambda s: (priority_rank.get(str(s["priority"]), 2), -int(s.get("order") or 0)))
        for s in candidates:
            if total_tokens <= budget_tokens:
                break
            if not str(s.get("text_after") or "").strip():
                continue
            if s["priority"] == "must":
                continue
            total_tokens -= int(s["tokens_after"])
            s["text_after"] = ""
            s["tokens_after"] = 0
            s["dropped"] = True
            s["reason"] = (str(s["reason"]) + ";" if s.get("reason") else "") + "dropped_for_budget"

        if total_tokens > budget_tokens:
            trim_candidates = [s for s in block_states if int(s["tokens_after"]) > 0 and str(s.get("text_after") or "").strip()]
            trim_candidates.sort(key=lambda s: (priority_rank.get(str(s["priority"]), 2), -int(s.get("order") or 0)))
            for s in trim_candidates:
                if total_tokens <= budget_tokens:
                    break
                need = total_tokens - budget_tokens
                current = int(s["tokens_after"])
                target = max(0, current - need)
                if target >= current:
                    continue
                trimmed_text = trim_text_to_tokens(str(s["text_after"] or ""), target)
                new_tokens = estimate_tokens(trimmed_text)
                if new_tokens >= current:
                    continue
                total_tokens -= current - new_tokens
                s["text_after"] = trimmed_text
                s["tokens_after"] = new_tokens
                s["trimmed"] = True
                s["reason"] = (str(s["reason"]) + ";" if s.get("reason") else "") + f"trim_to_fit:{target}"

    rendered_blocks: list[RenderedBlock] = []
    relative_messages: list[ChatMessage] = []
    absolute_items: list[dict] = []
    for s in block_states:
        rendered_blocks.append(
            RenderedBlock(
                id=str(s["id"]),
                identifier=str(s["identifier"]),
                role=str(s["role"]),
                enabled=bool(s["enabled"]),
                text=str(s["text_after"] or ""),
                missing=list(s.get("missing") or []),
                token_estimate=int(s.get("tokens_after") or 0),
            )
        )
        text_after = str(s.get("text_after") or "")
        if not text_after.strip():
            continue
        msg = ChatMessage(role=normalize_role(str(s.get("role") or "")), content=text_after)
        position = str(s.get("injection_position") or "relative").strip().lower()
        depth_raw = s.get("injection_depth")
        depth = int(depth_raw) if isinstance(depth_raw, int) and depth_raw >= 0 else 0
        if position == "absolute":
            absolute_items.append({"depth": depth, "order": int(s.get("order") or 0), "msg": msg})
        else:
            relative_messages.append(msg)

    messages = list(relative_messages)
    absolute_items.sort(key=lambda item: (-int(item.get("depth") or 0), int(item.get("order") or 0)))
    for item in absolute_items:
        depth = int(item.get("depth") or 0)
        idx = max(0, len(messages) - depth)
        messages.insert(idx, item["msg"])

    system = "\n\n".join([m.content for m in messages if m.role == "system" and m.content.strip()])
    user = flatten_messages([m for m in messages if m.role != "system"])

    render_log = {
        "task": task,
        "preset_id": preset.id,
        "context_optimizer": optimizer_log,
        "unified_context_budget": unified_log,
        "cache_hit": cache_hit,
        "cache_miss": cache_miss,
        "prompt_budget_tokens": budget_tokens,
        "prompt_budget_source": budget_source,
        "prompt_budget_calc": budget_calc,
        "prompt_tokens_estimate": total_tokens,
        "missing": sorted(all_missing),
        "blocks": [
            {
                "id": s["id"],
                "identifier": s["identifier"],
                "role": s["role"],
                "priority": s["priority"],
                "max_tokens": s["max_tokens"],
                "missing": s.get("missing") or [],
                "render_error": s.get("render_error"),
                "tokens_before": s["tokens_before"],
                "tokens_after": s["tokens_after"],
                "trimmed": s["trimmed"],
                "dropped": s["dropped"],
                "reason": s["reason"],
            }
            for s in block_states
        ],
    }

    return system, user, messages, sorted(all_missing), rendered_blocks, preset.id, render_log
