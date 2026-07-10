from __future__ import annotations

from dataclasses import dataclass

from app.services.llm_task_catalog import LLM_TASK_KEY_SET


@dataclass(frozen=True, slots=True)
class PromptTaskCatalogItem:
    key: str
    ui_copy_key: str
    e2e_specs: tuple[str, ...]


PROMPT_TASK_CATALOG: tuple[PromptTaskCatalogItem, ...] = (
    PromptTaskCatalogItem(
        key="outline_generate",
        ui_copy_key="outlineGenerate",
        e2e_specs=("test/specs/api/prompt-task-reachability.contract.spec.ts",),
    ),
    PromptTaskCatalogItem(
        key="chapter_generate",
        ui_copy_key="chapterGenerate",
        e2e_specs=("test/specs/api/prompt-task-reachability.contract.spec.ts",),
    ),
    PromptTaskCatalogItem(
        key="plan_chapter",
        ui_copy_key="planChapter",
        e2e_specs=("test/specs/api/prompt-task-reachability.contract.spec.ts",),
    ),
    PromptTaskCatalogItem(
        key="post_edit",
        ui_copy_key="postEdit",
        e2e_specs=("test/specs/api/prompt-task-reachability.contract.spec.ts",),
    ),
    PromptTaskCatalogItem(
        key="content_optimize",
        ui_copy_key="contentOptimize",
        e2e_specs=("test/specs/api/prompt-task-reachability.contract.spec.ts",),
    ),
)

PROMPT_TASK_KEYS: tuple[str, ...] = tuple(item.key for item in PROMPT_TASK_CATALOG)
PROMPT_TASK_SET: frozenset[str] = frozenset(PROMPT_TASK_KEYS)

_UNKNOWN_LLM_TASKS = PROMPT_TASK_SET - LLM_TASK_KEY_SET
if _UNKNOWN_LLM_TASKS:
    unknown = ", ".join(sorted(_UNKNOWN_LLM_TASKS))
    raise RuntimeError(f"Prompt task catalog contains unsupported LLM tasks: {unknown}")


def is_supported_prompt_task(task: str) -> bool:
    return str(task or "").strip() in PROMPT_TASK_SET
