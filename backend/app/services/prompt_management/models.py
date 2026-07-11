from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class _PromptImportAllState:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    conflicts: list[dict[str, object]] = field(default_factory=list)
    actions: list[dict[str, object]] = field(default_factory=list)
