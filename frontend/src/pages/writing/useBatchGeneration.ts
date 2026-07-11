import { toastApiError } from "../../lib/apiErrorPresentation";
import { toApiError } from "../../services/apiError";
import { useCallback, useEffect, useRef, useState } from "react";
import type { SetURLSearchParams } from "react-router-dom";

import type { BatchGenerationTask, BatchGenerationTaskItem, GenerateForm } from "../../components/writing/types";
import { useProjectTaskLiveSync, useProjectTaskRuntimeResource } from "../../hooks/useProjectTaskRuntimeResource";
import { createRequestSeqGuard } from "../../lib/requestSeqGuard";
import { ApiError, apiJson } from "../../services/apiClient";
import {
  cancelBatchGenerationTask,
  getActiveBatchGenerationTask,
  getBatchGenerationTask,
  hasFailedBatchGenerationItems,
  isBatchGenerationProjectTaskKind,
  isBatchGenerationTaskStatusRecoverable,
  pauseBatchGenerationTask,
  resumeBatchGenerationTask,
  retryFailedBatchGenerationTask,
  skipFailedBatchGenerationTask,
} from "../../services/projectTaskRuntime";
import type { Chapter, ChapterListItem, LLMPreset } from "../../types";
import { extractMissingNumbers } from "./writingErrorUtils";

export function useBatchGeneration(args: {
  projectId: string | undefined;
  preset: LLMPreset | null;
  activeChapter: Chapter | null;
  chapters: ChapterListItem[];
  genForm: GenerateForm;
  searchParams: URLSearchParams;
  setSearchParams: SetURLSearchParams;
  requestSelectChapter: (chapterId: string) => Promise<void>;
  toast: {
    toastError: (message: string, requestId?: string, action?: { label: string; onClick: () => void }) => void;
    toastSuccess: (message: string, requestId?: string) => void;
  };
}) {
  const {
    projectId,
    preset,
    activeChapter,
    chapters,
    genForm,
    searchParams,
    setSearchParams,
    requestSelectChapter,
    toast,
  } = args;

  const [open, setOpen] = useState(false);
  const [batchLoading, setBatchLoading] = useState(false);
  const [batchCount, setBatchCount] = useState(3);
  const [batchIncludeExisting, setBatchIncludeExisting] = useState(false);
  const [batchTask, setBatchTask] = useState<BatchGenerationTask | null>(null);
  const [batchItems, setBatchItems] = useState<BatchGenerationTaskItem[]>([]);

  const batchTaskRef = useRef<BatchGenerationTask | null>(null);
  const batchRefreshGuardRef = useRef(createRequestSeqGuard());
  const batchProjectTaskId = String(batchTask?.project_task_id || "").trim() || null;
  const batchRuntimeResource = useProjectTaskRuntimeResource({
    taskId: batchProjectTaskId,
    enabled: Boolean(batchProjectTaskId),
  });

  useEffect(() => {
    batchTaskRef.current = batchTask;
  }, [batchTask]);

  useEffect(() => {
    const batchRefreshGuard = batchRefreshGuardRef.current;
    return () => {
      batchRefreshGuard.invalidate();
    };
  }, []);

  const refreshBatchTask = useCallback(
    async (opts?: { silent?: boolean; taskId?: string | null }) => {
      if (!projectId) return;
      const seq = batchRefreshGuardRef.current.next();
      const fallbackTaskId = String(opts?.taskId || batchTaskRef.current?.id || "").trim();
      try {
        let data = await getActiveBatchGenerationTask(projectId);
        if (!data.task && fallbackTaskId) {
          try {
            data = await getBatchGenerationTask(fallbackTaskId);
          } catch (detailError) {
            if (!(detailError instanceof ApiError) || detailError.status !== 404) {
              throw detailError;
            }
          }
        }
        if (!batchRefreshGuardRef.current.isLatest(seq)) return;
        setBatchTask(data.task);
        setBatchItems(data.items);
        batchTaskRef.current = data.task;
      } catch (e) {
        if (!batchRefreshGuardRef.current.isLatest(seq)) return;
        if (!opts?.silent) {
          const err = toApiError(e);
          toastApiError(toast, err);
        }
      }
    },
    [projectId, toast],
  );

  useEffect(() => {
    if (!projectId) {
      batchRefreshGuardRef.current.invalidate();
      setBatchTask(null);
      setBatchItems([]);
      return;
    }
    void refreshBatchTask({ silent: true });
  }, [projectId, refreshBatchTask]);

  const projectTaskEvents = useProjectTaskLiveSync({
    projectId,
    enabled: Boolean(projectId),
    trackedTaskId: batchProjectTaskId,
    pollWhen: Boolean(batchTask && isBatchGenerationTaskStatusRecoverable(batchTask.status)),
    pollIntervalMs: 2000,
    refreshOnIdleSnapshot: true,
    pickSnapshotTaskId: (snapshot) => {
      const activeBatchTask = (snapshot.active_tasks || []).find((task) => isBatchGenerationProjectTaskKind(task.kind));
      return activeBatchTask?.id ?? null;
    },
    shouldRefreshOnEvent: (event, trackedTaskId) => {
      if (!isBatchGenerationProjectTaskKind(event.kind)) return false;
      return !trackedTaskId || trackedTaskId === event.task_id;
    },
    onRefresh: (taskId) => {
      void refreshBatchTask({ silent: true, taskId });
      void batchRuntimeResource.refresh({ taskId: taskId ?? batchProjectTaskId, force: true, silent: true });
    },
  });

  const openModal = useCallback(() => {
    setOpen(true);
    void refreshBatchTask();
  }, [refreshBatchTask]);

  const closeModal = useCallback(() => setOpen(false), []);

  const startBatchGeneration = useCallback(async () => {
    if (!projectId) return;
    if (!preset) {
      toast.toastError("Please save an LLM preset on the Prompts page first.");
      return;
    }
    setBatchLoading(true);
    try {
      const headers: Record<string, string> = { "X-LLM-Provider": preset.provider };
      const safeTargetWordCount =
        typeof genForm.target_word_count === "number" && genForm.target_word_count >= 100
          ? genForm.target_word_count
          : null;
      const payload = {
        after_chapter_id: activeChapter?.id ?? null,
        count: batchCount,
        include_existing: batchIncludeExisting,
        instruction: genForm.instruction,
        target_word_count: safeTargetWordCount,
        style_id: genForm.style_id,
        context: {
          include_world_setting: genForm.context.include_world_setting,
          include_style_guide: genForm.context.include_style_guide,
          include_constraints: genForm.context.include_constraints,
          include_outline: genForm.context.include_outline,
          include_smart_context: genForm.context.include_smart_context,
          require_sequential: true,
          character_ids: genForm.context.character_ids,
          entry_ids: genForm.context.entry_ids,
          previous_chapter: genForm.previous_mode === "full" ? "content" : "summary",
        },
      };

      const res = await apiJson<{ task: BatchGenerationTask; items: BatchGenerationTaskItem[] }>(
        `/api/projects/${projectId}/batch_generation_tasks`,
        { method: "POST", headers, body: JSON.stringify(payload) },
      );
      setBatchTask(res.data.task);
      setBatchItems(res.data.items);
      batchTaskRef.current = res.data.task;
      toast.toastSuccess("Batch generation started.", res.request_id);
    } catch (e) {
      const err = toApiError(e);
      const missingNumbers = extractMissingNumbers(err);
      if (missingNumbers.length > 0) {
        const targetNumber = missingNumbers[0]!;
        const target = chapters.find((chapter) => chapter.number === targetNumber);
        toast.toastError(
          `Missing prerequisite chapter content: ${missingNumbers.join(", ")}.`,
          err.requestId,
          target
            ? {
                label: `Open chapter ${targetNumber}`,
                onClick: () => void requestSelectChapter(target.id),
              }
            : undefined,
        );
        return;
      }
      toastApiError(toast, err);
    } finally {
      setBatchLoading(false);
    }
  }, [
    activeChapter?.id,
    batchCount,
    batchIncludeExisting,
    chapters,
    genForm,
    preset,
    projectId,
    requestSelectChapter,
    toast,
  ]);

  const cancelBatchGeneration = useCallback(async () => {
    if (!batchTask) return;
    setBatchLoading(true);
    try {
      await cancelBatchGenerationTask(batchTask.id);
      toast.toastSuccess("Batch generation canceled.");
      await refreshBatchTask({ silent: true });
      await batchRuntimeResource.refresh({ force: true, silent: true });
    } catch (e) {
      const err = toApiError(e);
      toastApiError(toast, err);
    } finally {
      setBatchLoading(false);
    }
  }, [batchRuntimeResource, batchTask, refreshBatchTask, toast]);

  const pauseBatchGeneration = useCallback(async () => {
    if (!batchTask) return;
    setBatchLoading(true);
    try {
      await pauseBatchGenerationTask(batchTask.id);
      toast.toastSuccess("Batch generation paused.");
      await refreshBatchTask({ silent: true });
      await batchRuntimeResource.refresh({ force: true, silent: true });
    } catch (e) {
      const err = toApiError(e);
      toastApiError(toast, err);
    } finally {
      setBatchLoading(false);
    }
  }, [batchRuntimeResource, batchTask, refreshBatchTask, toast]);

  const resumeBatchGeneration = useCallback(async () => {
    if (!batchTask) return;
    setBatchLoading(true);
    try {
      await resumeBatchGenerationTask(batchTask.id);
      toast.toastSuccess("Batch generation resumed.");
      await refreshBatchTask({ silent: true });
      await batchRuntimeResource.refresh({ force: true, silent: true });
    } catch (e) {
      const err = toApiError(e);
      toastApiError(toast, err);
    } finally {
      setBatchLoading(false);
    }
  }, [batchRuntimeResource, batchTask, refreshBatchTask, toast]);

  const retryFailedBatchGeneration = useCallback(async () => {
    if (!batchTask) return;
    setBatchLoading(true);
    try {
      await retryFailedBatchGenerationTask(batchTask.id);
      toast.toastSuccess("Failed chapters queued for retry.");
      await refreshBatchTask({ silent: true });
      await batchRuntimeResource.refresh({ force: true, silent: true });
    } catch (e) {
      const err = toApiError(e);
      toastApiError(toast, err);
    } finally {
      setBatchLoading(false);
    }
  }, [batchRuntimeResource, batchTask, refreshBatchTask, toast]);

  const skipFailedBatchGeneration = useCallback(async () => {
    if (!batchTask) return;
    setBatchLoading(true);
    try {
      await skipFailedBatchGenerationTask(batchTask.id);
      toast.toastSuccess("Failed chapters skipped.");
      await refreshBatchTask({ silent: true });
      await batchRuntimeResource.refresh({ force: true, silent: true });
    } catch (e) {
      const err = toApiError(e);
      toastApiError(toast, err);
    } finally {
      setBatchLoading(false);
    }
  }, [batchRuntimeResource, batchTask, refreshBatchTask, toast]);

  const applyBatchItemToEditor = useCallback(
    async (item: BatchGenerationTaskItem) => {
      if (!item.chapter_id || !item.generation_run_id) return;
      setOpen(false);
      await requestSelectChapter(item.chapter_id);
      const next = new URLSearchParams(searchParams);
      next.set("applyRunId", item.generation_run_id);
      setSearchParams(next, { replace: true });
    },
    [requestSelectChapter, searchParams, setSearchParams],
  );

  return {
    open,
    openModal,
    closeModal,
    batchLoading,
    batchCount,
    setBatchCount,
    batchIncludeExisting,
    setBatchIncludeExisting,
    batchTask,
    batchItems,
    batchRuntime: batchRuntimeResource.data,
    projectTaskStreamStatus: projectTaskEvents.status,
    refreshBatchTask,
    startBatchGeneration,
    cancelBatchGeneration,
    pauseBatchGeneration,
    resumeBatchGeneration,
    retryFailedBatchGeneration,
    skipFailedBatchGeneration,
    hasFailedBatchItems: hasFailedBatchGenerationItems(batchItems),
    applyBatchItemToEditor,
  };
}
