import { toApiError } from "../../services/apiError";
import { toastApiError } from "../../lib/apiErrorPresentation";
import type { ComponentProps } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";

import { WizardNextBar } from "../../components/atelier/WizardNextBar";
import { useConfirm } from "../../components/ui/confirm";
import { useToast } from "../../components/ui/toast";
import { usePersistentOutletIsActive } from "../../hooks/usePersistentOutlet";
import { useProjectData } from "../../hooks/useProjectData";
import { useWizardProgress } from "../../hooks/useWizardProgress";
import { apiJson, type ApiError } from "../../services/apiClient";
import { listEntries, type EntryItem } from "../../services/entriesApi";
import { getWizardProjectChangedAt } from "../../services/wizard";
import type { Character, LLMPreset, Outline, OutlineListItem } from "../../types";

import type {
  WritingChapterListDrawerProps,
  WritingEditorSectionProps,
  WritingPageOverlaysProps,
  WritingStreamFloatingCardProps,
  WritingWorkspaceProps,
} from "./WritingPageSections";
import { useApplyGenerationRun } from "./useApplyGenerationRun";
import { useBatchGeneration } from "./useBatchGeneration";
import { useChapterCrud } from "./useChapterCrud";
import { useChapterEditor } from "./useChapterEditor";
import { useChapterGeneration } from "./useChapterGeneration";
import { useGenerationHistory } from "./useGenerationHistory";
import { useOutlineSwitcher } from "./useOutlineSwitcher";
import type { ChapterForm } from "./writingUtils";
import { type ChapterAutoUpdatesTriggerResult } from "./writingPageModels";
import {
  getWritingGenerateIndicatorLabel,
  getWritingNextChapterReplaceTitle,
  WRITING_PAGE_COPY,
} from "./writingPageCopy";

type WritingLoaded = {
  outlines: OutlineListItem[];
  outline: Outline;
  preset: LLMPreset;
  characters: Character[];
  entries: EntryItem[];
};

export function deriveWritingMetadataLoadState<T>(query: { data: T | null; error: ApiError | null; loading: boolean }) {
  return {
    loading: query.loading && query.data === null,
    blockingLoadError: query.data === null ? query.error : null,
    refreshLoadError: query.data !== null ? query.error : null,
  };
}

export type WritingPageState = {
  loading: boolean;
  metadataBlockingLoadError: ApiError | null;
  metadataRefreshLoadError: ApiError | null;
  reloadMetadata: () => Promise<void>;
  chapterListBlockingLoadError: ApiError | null;
  chapterListRefreshLoadError: ApiError | null;
  reloadChapterList: () => Promise<void>;
  dirty: boolean;
  showUnsavedGuard: boolean;
  workspaceProps: WritingWorkspaceProps;
  chapterListDrawerProps: WritingChapterListDrawerProps;
  overlaysProps: WritingPageOverlaysProps;
  streamFloatingProps: WritingStreamFloatingCardProps;
  wizardBarProps: ComponentProps<typeof WizardNextBar>;
};

export function useWritingPageState(): WritingPageState {
  const { projectId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedChapterId = searchParams.get("chapterId");
  const applyRunId = searchParams.get("applyRunId");
  const toast = useToast();
  const confirm = useConfirm();
  const outletActive = usePersistentOutletIsActive();
  const wizard = useWizardProgress(projectId);
  const refreshWizard = wizard.refresh;
  const bumpWizardLocal = wizard.bumpLocal;
  const lastProjectChangedAtRef = useRef<string | null>(null);

  const [chapterListOpen, setChapterListOpen] = useState(false);
  const [contentEditorTab, setContentEditorTab] = useState<"edit" | "preview">("edit");
  const autoGenerateNextRef = useRef<{ chapterId: string; mode: "replace" | "append" } | null>(null);

  const [aiOpen, setAiOpen] = useState(false);
  const [promptInspectorOpen, setPromptInspectorOpen] = useState(false);
  const [autoUpdatesTriggering, setAutoUpdatesTriggering] = useState(false);

  const writingQuery = useProjectData<WritingLoaded>(projectId, async (id) => {
    const loadEntries = async (): Promise<EntryItem[]> => {
      const items: EntryItem[] = [];
      let offset = 0;
      while (true) {
        const page = await listEntries(id, { limit: 200, offset });
        items.push(...page.items);
        if (typeof page.next_offset !== "number") break;
        offset = page.next_offset;
      }
      return items;
    };

    const [outlineRes, presetRes, charactersRes, entries] = await Promise.all([
      apiJson<{ outline: Outline }>(`/api/projects/${id}/outline`),
      apiJson<{ llm_preset: LLMPreset }>(`/api/projects/${id}/llm_preset`),
      apiJson<{ characters: Character[] }>(`/api/projects/${id}/characters`),
      loadEntries(),
    ]);
    const outlinesRes = await apiJson<{ outlines: OutlineListItem[] }>(`/api/projects/${id}/outlines`);
    return {
      outlines: outlinesRes.data.outlines,
      outline: outlineRes.data.outline,
      preset: presetRes.data.llm_preset,
      characters: charactersRes.data.characters,
      entries,
    };
  });
  const outlines = writingQuery.data?.outlines ?? [];
  const outline = writingQuery.data?.outline ?? null;
  const characters = writingQuery.data?.characters ?? [];
  const entries = writingQuery.data?.entries ?? [];
  const preset = writingQuery.data?.preset ?? null;
  const refreshWriting = writingQuery.refresh;
  const resetWritingError = writingQuery.resetError;
  const reloadMetadata = useCallback(async () => {
    resetWritingError();
    await refreshWriting();
  }, [refreshWriting, resetWritingError]);

  const chapterEditor = useChapterEditor({
    projectId,
    requestedChapterId,
    searchParams,
    setSearchParams,
    toast,
    confirm,
    refreshWizard,
    bumpWizardLocal,
  });
  const {
    loading: chapterListLoading,
    chapters,
    chapterListError,
    chapterListHasData,
    chapterListHasLoaded,
    refreshChapters,
    retryChapterList,
    activeId,
    setActiveId,
    activeChapter,
    baseline,
    form,
    setForm,
    dirty,
    saveChapter,
    requestSelectChapter: requestSelectChapterBase,
    loadingChapter,
    chapterLoadError,
    retryChapter,
    saving,
  } = chapterEditor;

  useEffect(() => {
    if (!projectId) {
      lastProjectChangedAtRef.current = null;
      return;
    }
    lastProjectChangedAtRef.current = getWizardProjectChangedAt(projectId);
  }, [projectId]);

  useEffect(() => {
    if (!projectId || !outletActive || dirty) return;
    const changedAt = getWizardProjectChangedAt(projectId);
    if ((changedAt ?? null) === (lastProjectChangedAtRef.current ?? null)) return;
    lastProjectChangedAtRef.current = changedAt;
    void refreshWriting();
    void refreshChapters();
    void refreshWizard();
  }, [dirty, outletActive, projectId, refreshChapters, refreshWriting, refreshWizard]);

  useEffect(() => {
    if (!activeChapter) autoGenerateNextRef.current = null;
  }, [activeChapter]);

  const isDoneReadonly = Boolean(baseline && form && baseline.status === "done" && form.status === "done");

  useApplyGenerationRun({
    applyRunId,
    activeChapter,
    form,
    dirty,
    confirm,
    toast,
    saveChapter,
    searchParams,
    setSearchParams,
    setForm,
  });

  const requestSelectChapter = useCallback(
    async (chapterId: string) => {
      autoGenerateNextRef.current = null;
      await requestSelectChapterBase(chapterId);
    },
    [requestSelectChapterBase],
  );

  const chapterCrud = useChapterCrud({
    projectId,
    chapters,
    activeChapter,
    setActiveId,
    requestSelectChapter,
    toast,
    confirm,
    bumpWizardLocal,
    refreshWizard,
  });

  const generation = useChapterGeneration({
    projectId,
    activeChapter,
    chapters,
    form,
    setForm,
    preset,
    dirty,
    saveChapter,
    requestSelectChapter,
    toast,
    confirm,
  });
  const { generating, genRequestId, genStreamProgress, genForm, setGenForm, generate, abortGenerate } = generation;

  const batch = useBatchGeneration({
    projectId,
    preset,
    activeChapter,
    chapters,
    genForm,
    searchParams,
    setSearchParams,
    requestSelectChapter,
    toast,
  });
  const history = useGenerationHistory({ projectId, toast });

  const activeOutlineId = outline?.id ?? "";
  const switchOutline = useOutlineSwitcher({
    projectId,
    activeOutlineId,
    dirty,
    confirm,
    toast,
    saveChapter,
    bumpWizardLocal,
    refreshWizard,
    refreshChapters,
    refreshWriting,
  });

  const saveAndTriggerAutoUpdates = useCallback(async () => {
    if (!projectId || !activeChapter || autoUpdatesTriggering || !dirty) return;

    setAutoUpdatesTriggering(true);
    try {
      const ok = await saveChapter({ silent: true });
      if (!ok) return;

      const response = await apiJson<ChapterAutoUpdatesTriggerResult>(
        `/api/chapters/${activeChapter.id}/trigger_auto_updates`,
        {
          method: "POST",
          body: JSON.stringify({}),
        },
      );
      toast.toastSuccess(WRITING_PAGE_COPY.autoUpdatesCreated, response.request_id);
    } catch (error) {
      const err = toApiError(error);
      toastApiError(toast, err);
    } finally {
      setAutoUpdatesTriggering(false);
    }
  }, [activeChapter, autoUpdatesTriggering, dirty, projectId, saveChapter, toast]);

  const saveAndGenerateNext = useCallback(async () => {
    if (!activeChapter) return;

    const ok = await saveChapter();
    if (!ok) return;

    const sorted = [...chapters].sort((a, b) => (a.number ?? 0) - (b.number ?? 0));
    const currentIndex = sorted.findIndex((chapter) => chapter.id === activeChapter.id);
    const nextChapter =
      currentIndex >= 0
        ? (sorted[currentIndex + 1] ?? null)
        : (sorted.find((chapter) => (chapter.number ?? 0) > (activeChapter.number ?? 0)) ?? null);

    if (!nextChapter) {
      toast.toastSuccess(WRITING_PAGE_COPY.saveAndGenerateLastChapter);
      return;
    }

    const nextHasContent = Boolean(nextChapter.has_content || nextChapter.has_summary);
    if (nextHasContent) {
      const replaceOk = await confirm.confirm({
        title: getWritingNextChapterReplaceTitle(nextChapter.number),
        description: WRITING_PAGE_COPY.confirms.nextChapterReplace.description,
        confirmText: WRITING_PAGE_COPY.confirms.nextChapterReplace.confirmText,
        cancelText: WRITING_PAGE_COPY.confirms.nextChapterReplace.cancelText,
        danger: true,
      });
      if (!replaceOk) return;
    }

    autoGenerateNextRef.current = { chapterId: nextChapter.id, mode: "replace" };
    setActiveId(nextChapter.id);
    setAiOpen(true);
  }, [activeChapter, chapters, confirm, saveChapter, setActiveId, toast]);

  useEffect(() => {
    const pending = autoGenerateNextRef.current;
    if (!pending || !activeChapter || !form || generating) return;
    if (activeChapter.id !== pending.chapterId) return;
    autoGenerateNextRef.current = null;
    void generate(pending.mode);
  }, [activeChapter, form, generate, generating]);

  const workspaceProps: WritingWorkspaceProps = {
    toolbarProps: {
      outlines,
      activeOutlineId,
      chaptersCount: chapters.length,
      batchProgressText:
        batch.batchTask && (batch.batchTask.status === "queued" || batch.batchTask.status === "running")
          ? `（${batch.batchTask.completed_count}/${batch.batchTask.total_count}）`
          : "",
      aiGenerateDisabled: !activeChapter || loadingChapter,
      onSwitchOutline: (outlineId) => void switchOutline(outlineId),
      onOpenBatch: batch.openModal,
      onOpenHistory: history.openDrawer,
      onOpenAiGenerate: () => setAiOpen(true),
      onCreateChapter: chapterCrud.openCreate,
    },
    chapterListProps: {
      chapters,
      activeId,
      onSelectChapter: (chapterId) => void requestSelectChapter(chapterId),
      onOpenDrawer: () => setChapterListOpen(true),
    },
    editorProps: {
      activeChapter,
      form,
      dirty,
      isDoneReadonly,
      loadingChapter,
      loadError: chapterLoadError,
      generating,
      saving,
      autoUpdatesTriggering,
      contentEditorTab,
      onContentEditorTabChange: setContentEditorTab,
      onTitleChange: (value) => setForm((prev) => (prev ? { ...prev, title: value } : prev)),
      onStatusChange: (status) => setForm((prev) => (prev ? { ...prev, status } : prev)),
      onPlanChange: (value) => setForm((prev) => (prev ? { ...prev, plan: value } : prev)),
      onContentChange: (value) => setForm((prev) => (prev ? { ...prev, content_md: value } : prev)),
      onSummaryChange: (value) => setForm((prev) => (prev ? { ...prev, summary: value } : prev)),
      onDeleteChapter: () => void chapterCrud.deleteChapter(),
      onRetryLoad: () => void retryChapter(),
      onSaveAndTriggerAutoUpdates: () => void saveAndTriggerAutoUpdates(),
      onSaveChapter: () => void saveChapter(),
      onReopenDrafting: () => setForm((prev: ChapterForm | null) => (prev ? { ...prev, status: "drafting" } : prev)),
      generationIndicatorLabel:
        genForm.stream && genStreamProgress
          ? getWritingGenerateIndicatorLabel(genStreamProgress.message, genStreamProgress.progress)
          : undefined,
    } satisfies WritingEditorSectionProps,
  };

  const chapterListDrawerProps: WritingChapterListDrawerProps = {
    open: chapterListOpen,
    chapters,
    activeId,
    onClose: () => setChapterListOpen(false),
    onSelectChapter: (chapterId) => void requestSelectChapter(chapterId),
  };

  const overlaysProps: WritingPageOverlaysProps = {
    createChapterDialogProps: {
      open: chapterCrud.createOpen,
      saving: chapterCrud.createSaving,
      form: chapterCrud.createForm,
      setForm: chapterCrud.setCreateForm,
      onClose: () => chapterCrud.setCreateOpen(false),
      onSubmit: () => void chapterCrud.createChapter(),
    },
    batchGenerationModalProps: {
      open: batch.open,
      batchLoading: batch.batchLoading,
      activeChapterNumber: activeChapter?.number ?? null,
      batchCount: batch.batchCount,
      setBatchCount: batch.setBatchCount,
      batchIncludeExisting: batch.batchIncludeExisting,
      setBatchIncludeExisting: batch.setBatchIncludeExisting,
      batchTask: batch.batchTask,
      batchItems: batch.batchItems,
      batchRuntime: batch.batchRuntime,
      projectTaskStreamStatus: batch.projectTaskStreamStatus,
      onClose: batch.closeModal,
      onCancelTask: () => void batch.cancelBatchGeneration(),
      onPauseTask: () => void batch.pauseBatchGeneration(),
      onResumeTask: () => void batch.resumeBatchGeneration(),
      onRetryFailedTask: () => void batch.retryFailedBatchGeneration(),
      onSkipFailedTask: () => void batch.skipFailedBatchGeneration(),
      onStartTask: () => void batch.startBatchGeneration(),
      onApplyItemToEditor: (item) => void batch.applyBatchItemToEditor(item),
    },
    aiGenerateDrawerProps: {
      open: aiOpen,
      generating,
      preset,
      projectId,
      activeChapter: Boolean(activeChapter),
      dirty,
      saving: saving || loadingChapter,
      genForm,
      setGenForm,
      characters,
      entries,
      streamProgress: genStreamProgress,
      onClose: () => setAiOpen(false),
      onSave: () => void saveChapter(),
      onSaveAndGenerateNext: () => void saveAndGenerateNext(),
      onGenerateAppend: () => void generate("append"),
      onGenerateReplace: () => void generate("replace"),
      onCancelGenerate: abortGenerate,
      onOpenPromptInspector: () => setPromptInspectorOpen(true),
    },
    promptInspectorDrawerProps: {
      open: promptInspectorOpen,
      onClose: () => setPromptInspectorOpen(false),
      preset,
      chapterId: activeChapter?.id ?? undefined,
      draftContentMd: form?.content_md ?? "",
      generating,
      genForm,
      setGenForm,
      onGenerate: generate,
    },
    generationHistoryDrawerProps: {
      open: history.open,
      onClose: history.closeDrawer,
      loading: history.runsLoading,
      runs: history.runs,
      selectedRun: history.selectedRun,
      onSelectRun: (run) => void history.selectRun(run),
    },
  };

  const streamFloatingProps: WritingStreamFloatingCardProps = {
    open: generating && genForm.stream && !aiOpen,
    requestId: genRequestId,
    message: genStreamProgress?.message,
    progress: genStreamProgress?.progress ?? 0,
    onExpand: () => setAiOpen(true),
    onCancel: abortGenerate,
  };

  const metadataLoadState = deriveWritingMetadataLoadState(writingQuery);
  const chapterListBlockingLoadError =
    writingQuery.data !== null && !chapterListHasData && chapterListHasLoaded ? chapterListError : null;
  const chapterListRefreshLoadError = writingQuery.data !== null && chapterListHasData ? chapterListError : null;
  const reloadChapterList = useCallback(async () => {
    try {
      await retryChapterList();
    } catch {
      // The chapter store snapshot owns the retry error state.
    }
  }, [retryChapterList]);

  return {
    loading: metadataLoadState.loading || (writingQuery.data !== null && chapterListLoading),
    metadataBlockingLoadError: metadataLoadState.blockingLoadError,
    metadataRefreshLoadError: metadataLoadState.refreshLoadError,
    reloadMetadata,
    chapterListBlockingLoadError,
    chapterListRefreshLoadError,
    reloadChapterList,
    dirty,
    showUnsavedGuard: dirty && outletActive,
    workspaceProps,
    chapterListDrawerProps,
    overlaysProps,
    streamFloatingProps,
    wizardBarProps: {
      projectId,
      currentStep: "writing",
      progress: wizard.progress,
      loading: wizard.loading,
      loadError: wizard.error,
      onRetryLoad: wizard.reload,
      retryBlockedReason: dirty ? "请先保存或放弃未保存修改再重试" : undefined,
      dirty,
      saving: saving || loadingChapter || generating,
      onSave: saveChapter,
    },
  };
}
