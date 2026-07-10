import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ComponentProps } from "react";
import { useParams } from "react-router-dom";

import { WizardNextBar } from "../../components/atelier/WizardNextBar";
import { useConfirm } from "../../components/ui/confirm";
import type { GenerationFloatingCardProps } from "../../components/ui/GenerationFloatingCard";
import { useToast } from "../../components/ui/toast";
import { useProjectData } from "../../hooks/useProjectData";
import { useAutoSave } from "../../hooks/useAutoSave";
import { usePersistentOutletIsActive } from "../../hooks/usePersistentOutlet";
import { useQueuedSave } from "../../hooks/useQueuedSave";
import { useSaveHotkey } from "../../hooks/useSaveHotkey";
import { useWizardProgress } from "../../hooks/useWizardProgress";
import { ApiError, apiJson } from "../../services/apiClient";
import { markWizardProjectChanged } from "../../services/wizard";
import type { LLMPreset, Outline, OutlineListItem, Project } from "../../types";
import { deriveOutlineFromStoredContent } from "../outlineParsing";

import type {
  OutlineActionsBarProps,
  OutlineEditorSectionProps,
  OutlineGenerationModalProps,
  OutlineHeaderSectionProps,
  OutlineParsingModalProps,
  OutlineTitleModalProps,
} from "./OutlinePageSections";
import { OUTLINE_COPY } from "./outlineCopy";
import { buildNextOutlineTitle } from "./outlineModels";
import { useDetailedOutlineState, type DetailedOutlineState } from "./useDetailedOutlineState";
import { useOutlineGenerationState } from "./useOutlineGenerationState";
import { useOutlineParsingState } from "./useOutlineParsingState";

type OutlineLoaded = {
  outlines: OutlineListItem[];
  outline: Outline;
  preset: LLMPreset;
};

type SaveOutline = (
  nextContent?: string,
  nextStructure?: unknown,
  opts?: { silent?: boolean; snapshotContent?: string },
) => Promise<boolean>;

export type OutlinePageState = {
  loading: boolean;
  dirty: boolean;
  showUnsavedGuard: boolean;
  headerProps: OutlineHeaderSectionProps;
  actionsBarProps: OutlineActionsBarProps;
  editorProps: OutlineEditorSectionProps;
  titleModalProps: OutlineTitleModalProps;
  generationModalProps: OutlineGenerationModalProps;
  parsingModalProps: OutlineParsingModalProps;
  wizardBarProps: ComponentProps<typeof WizardNextBar>;
  detailedOutlineState: DetailedOutlineState;
  outlineGenFloatingProps: GenerationFloatingCardProps;
  parsingFloatingProps: GenerationFloatingCardProps;
  detailedGenFloatingProps: GenerationFloatingCardProps;
  skeletonGenFloatingProps: GenerationFloatingCardProps;
  switchToDetailedRequested: boolean;
  clearSwitchToDetailedRequest: () => void;
};

export function useOutlinePageState(): OutlinePageState {
  const { projectId } = useParams();
  const toast = useToast();
  const confirm = useConfirm();
  const outletActive = usePersistentOutletIsActive();
  const wizard = useWizardProgress(projectId);
  const refreshWizard = wizard.refresh;
  const bumpWizardLocal = wizard.bumpLocal;

  const [outlines, setOutlines] = useState<OutlineListItem[]>([]);
  const [activeOutline, setActiveOutline] = useState<Outline | null>(null);
  const [preset, setPreset] = useState<LLMPreset | null>(null);
  const [baseline, setBaseline] = useState("");
  const [content, setContent] = useState("");
  const [switchToDetailedRequested, setSwitchToDetailedRequested] = useState(false);
  const [titleModal, setTitleModal] = useState<{ open: boolean; mode: "create" | "rename"; title: string }>({
    open: false,
    mode: "create",
    title: "",
  });

  const wizardRefreshTimerRef = useRef<number | null>(null);
  const baselineRef = useRef("");
  const pendingDetailedSwitchOutlineIdRef = useRef<string | null>(null);

  const outlineQuery = useProjectData<OutlineLoaded>(
    projectId,
    async (id) => {
      const [outlineResponse, presetResponse] = await Promise.all([
        apiJson<{ outline: Outline }>(`/api/projects/${id}/outline`),
        apiJson<{ llm_preset: LLMPreset }>(`/api/projects/${id}/llm_preset`),
      ]);
      const outlinesResponse = await apiJson<{ outlines: OutlineListItem[] }>(`/api/projects/${id}/outlines`);
      return {
        outlines: outlinesResponse.data.outlines,
        outline: outlineResponse.data.outline,
        preset: presetResponse.data.llm_preset,
      };
    },
    { toastOnError: true },
  );

  useEffect(() => {
    if (!outlineQuery.data) return;
    const normalizedStored = deriveOutlineFromStoredContent(
      outlineQuery.data.outline.content_md ?? "",
      outlineQuery.data.outline.structure,
    );
    setOutlines(outlineQuery.data.outlines);
    setActiveOutline({
      ...outlineQuery.data.outline,
      content_md: normalizedStored.normalizedContentMd,
      structure:
        normalizedStored.chapters.length > 0 || normalizedStored.volumes.length > 0
          ? { volumes: normalizedStored.volumes, chapters: normalizedStored.chapters }
          : outlineQuery.data.outline.structure,
    });
    setPreset(outlineQuery.data.preset);
    baselineRef.current = normalizedStored.normalizedContentMd;
    setBaseline(normalizedStored.normalizedContentMd);
    setContent(normalizedStored.normalizedContentMd);
  }, [outlineQuery.data]);

  useEffect(() => {
    return () => {
      if (wizardRefreshTimerRef.current !== null) {
        window.clearTimeout(wizardRefreshTimerRef.current);
        wizardRefreshTimerRef.current = null;
      }
    };
  }, []);

  const dirty = content !== baseline;

  const performSave = useCallback<SaveOutline>(
    async (nextContent, nextStructure, opts) => {
      if (!projectId) return false;

      const silent = Boolean(opts?.silent);
      const snapshotContent = opts?.snapshotContent;
      const toSave = snapshotContent ?? nextContent ?? content;
      if (nextStructure === undefined && toSave === baselineRef.current) {
        return true;
      }

      try {
        const scheduleWizardRefresh = () => {
          if (wizardRefreshTimerRef.current !== null) {
            window.clearTimeout(wizardRefreshTimerRef.current);
          }
          wizardRefreshTimerRef.current = window.setTimeout(() => void refreshWizard(), 1200);
        };

        const response = await apiJson<{ outline: Outline }>(`/api/projects/${projectId}/outline`, {
          method: "PUT",
          body: JSON.stringify({ content_md: toSave, structure: nextStructure }),
        });
        const savedContent = response.data.outline.content_md ?? "";
        baselineRef.current = savedContent;
        setBaseline(savedContent);
        setContent((prev) => {
          if (nextContent !== undefined) return savedContent;
          if (prev === toSave) return savedContent;
          return prev;
        });
        setActiveOutline(response.data.outline);
        markWizardProjectChanged(projectId);
        bumpWizardLocal();
        if (silent) {
          scheduleWizardRefresh();
        } else {
          await refreshWizard();
          toast.toastSuccess(OUTLINE_COPY.saveSuccess);
        }
        return true;
      } catch (error) {
        const err = error as ApiError;
        toast.toastError(`${err.message} (${err.code})`, err.requestId);
        return false;
      }
    },
    [bumpWizardLocal, content, projectId, refreshWizard, toast],
  );

  const { save: queueSave, saving } = useQueuedSave(performSave);
  const save = useCallback<SaveOutline>(
    (nextContent, nextStructure, opts) => {
      const snapshotContent = opts?.snapshotContent ?? nextContent ?? content;
      return queueSave(nextContent, nextStructure, { ...opts, snapshotContent });
    },
    [content, queueSave],
  );

  useSaveHotkey(() => void save(), dirty);

  useAutoSave({
    enabled: Boolean(projectId),
    dirty,
    delayMs: 900,
    getSnapshot: () => content,
    onSave: async (snapshot) => {
      await save(undefined, undefined, { silent: true, snapshotContent: snapshot });
    },
    deps: [content, projectId, activeOutline?.id ?? ""],
  });

  const refreshOutline = outlineQuery.refresh;
  const activeOutlineId = activeOutline?.id ?? "";

  const clearSwitchToDetailedRequest = useCallback(() => {
    setSwitchToDetailedRequested(false);
  }, []);

  const requestSwitchToDetailed = useCallback(
    (targetOutlineId?: string) => {
      if (!targetOutlineId) return;
      if (targetOutlineId === activeOutlineId) {
        pendingDetailedSwitchOutlineIdRef.current = null;
        setSwitchToDetailedRequested(true);
        return;
      }
      pendingDetailedSwitchOutlineIdRef.current = targetOutlineId;
    },
    [activeOutlineId],
  );

  useEffect(() => {
    if (!activeOutlineId) return;
    if (pendingDetailedSwitchOutlineIdRef.current !== activeOutlineId) return;
    pendingDetailedSwitchOutlineIdRef.current = null;
    setSwitchToDetailedRequested(true);
  }, [activeOutlineId]);

  const createOutline = useCallback(
    async (title: string, contentMd: string, structure: unknown) => {
      if (!projectId) return null;
      try {
        const response = await apiJson<{ outline: Outline }>(`/api/projects/${projectId}/outlines`, {
          method: "POST",
          body: JSON.stringify({ title, content_md: contentMd, structure }),
        });
        markWizardProjectChanged(projectId);
        bumpWizardLocal();
        await refreshOutline();
        await refreshWizard();
        toast.toastSuccess(OUTLINE_COPY.createdAndSwitched);
        return response.data.outline;
      } catch (error) {
        const err = error as ApiError;
        toast.toastError(`${err.message} (${err.code})`, err.requestId);
        return null;
      }
    },
    [bumpWizardLocal, projectId, refreshOutline, refreshWizard, toast],
  );

  const renameOutline = useCallback(
    async (title: string) => {
      if (!projectId || !activeOutlineId) return;
      try {
        await apiJson<{ outline: Outline }>(`/api/projects/${projectId}/outlines/${activeOutlineId}`, {
          method: "PUT",
          body: JSON.stringify({ title }),
        });
        markWizardProjectChanged(projectId);
        bumpWizardLocal();
        await refreshOutline();
        toast.toastSuccess(OUTLINE_COPY.renamed);
      } catch (error) {
        const err = error as ApiError;
        toast.toastError(`${err.message} (${err.code})`, err.requestId);
      }
    },
    [activeOutlineId, bumpWizardLocal, projectId, refreshOutline, toast],
  );

  const deleteOutline = useCallback(async () => {
    if (!projectId || !activeOutlineId) return;
    const ok = await confirm.confirm({ ...OUTLINE_COPY.confirms.deleteOutline, danger: true });
    if (!ok) return;
    try {
      await apiJson<Record<string, never>>(`/api/projects/${projectId}/outlines/${activeOutlineId}`, {
        method: "DELETE",
      });
      markWizardProjectChanged(projectId);
      bumpWizardLocal();
      await refreshOutline();
      await refreshWizard();
      toast.toastSuccess(OUTLINE_COPY.deleted);
    } catch (error) {
      const err = error as ApiError;
      toast.toastError(`${err.message} (${err.code})`, err.requestId);
    }
  }, [activeOutlineId, bumpWizardLocal, confirm, projectId, refreshOutline, refreshWizard, toast]);

  const switchOutline = useCallback(
    async (nextOutlineId: string) => {
      if (!projectId) return;
      if (!nextOutlineId || nextOutlineId === activeOutlineId) return;

      if (dirty) {
        const choice = await confirm.choose(OUTLINE_COPY.confirms.switchOutline);
        if (choice === "cancel") return;
        if (choice === "confirm") {
          const ok = await save();
          if (!ok) return;
        }
      }

      try {
        await apiJson<{ project: Project }>(`/api/projects/${projectId}`, {
          method: "PUT",
          body: JSON.stringify({ active_outline_id: nextOutlineId }),
        });
        markWizardProjectChanged(projectId);
        bumpWizardLocal();
        await refreshOutline();
        await refreshWizard();
        toast.toastSuccess(OUTLINE_COPY.switched);
      } catch (error) {
        const err = error as ApiError;
        toast.toastError(`${err.message} (${err.code})`, err.requestId);
      }
    },
    [activeOutlineId, bumpWizardLocal, confirm, dirty, projectId, refreshOutline, refreshWizard, save, toast],
  );

  const generation = useOutlineGenerationState({
    projectId,
    preset,
    dirty,
    save,
    createOutline,
    confirm,
    toast,
  });

  const parsing = useOutlineParsingState({
    projectId,
    outlineId: activeOutlineId || undefined,
    preset,
    dirty,
    save,
    createOutline,
    confirm,
    toast,
  });

  const detailedOutline = useDetailedOutlineState(projectId, activeOutlineId || undefined);

  const storedChapters = useMemo(
    () => deriveOutlineFromStoredContent(activeOutline?.content_md ?? "", activeOutline?.structure).chapters,
    [activeOutline?.content_md, activeOutline?.structure],
  );
  const previewChapters = generation.genPreview?.chapters;
  const chaptersForSkeleton = useMemo(
    () => (previewChapters && previewChapters.length > 0 ? previewChapters : storedChapters),
    [previewChapters, storedChapters],
  );
  const canCreateChapters = chaptersForSkeleton.length > 0;

  const openCreateTitleModal = useCallback(() => {
    setTitleModal({
      open: true,
      mode: "create",
      title: buildNextOutlineTitle(outlines.length),
    });
  }, [outlines.length]);

  const openRenameTitleModal = useCallback(() => {
    setTitleModal({
      open: true,
      mode: "rename",
      title: activeOutline?.title ?? "",
    });
  }, [activeOutline?.title]);

  const closeTitleModal = useCallback(() => {
    setTitleModal((prev) => ({ ...prev, open: false }));
  }, []);

  const confirmTitleModal = useCallback(async () => {
    const title = titleModal.title.trim();
    if (!title) {
      toast.toastError(OUTLINE_COPY.titleRequired);
      return;
    }

    if (titleModal.mode === "create") {
      if (dirty) {
        const choice = await confirm.choose(OUTLINE_COPY.confirms.titleModalContinue);
        if (choice === "cancel") return;
        if (choice === "confirm") {
          const ok = await save();
          if (!ok) return;
        }
      }
      closeTitleModal();
      await createOutline(title, "", null);
      return;
    }

    closeTitleModal();
    await renameOutline(title);
  }, [closeTitleModal, confirm, createOutline, dirty, renameOutline, save, titleModal.mode, titleModal.title, toast]);

  const outlineGenFloatingProps: GenerationFloatingCardProps = {
    open: generation.generating && !generation.open,
    title: "大纲生成中",
    message: generation.streamProgress?.message,
    progress: generation.streamProgress?.progress ?? 0,
    onExpand: () => generation.setOpen(true),
    onCancel: generation.cancelGenerate,
  };

  const parsingFloatingProps: GenerationFloatingCardProps = {
    open: parsing.parsing && !parsing.open,
    title: "智能解析中",
    message: parsing.parseProgress?.message,
    progress: parsing.parseProgress?.progress ?? 0,
    onExpand: parsing.openParseModal,
    onCancel: parsing.cancelParse,
  };

  const detailedGenFloatingProps: GenerationFloatingCardProps = {
    open: detailedOutline.generating && !detailedOutline.generateModalOpen,
    title: "细纲生成中",
    message: detailedOutline.progress?.message,
    progress: detailedOutline.progress
      ? detailedOutline.progress.total > 0
        ? (detailedOutline.progress.current / detailedOutline.progress.total) * 100
        : 0
      : 0,
    onExpand: detailedOutline.openGenerateModal,
    onCancel: detailedOutline.cancelGenerate,
  };

  const skeletonGenFloatingProps: GenerationFloatingCardProps = {
    open: detailedOutline.skeletonGenerating && !detailedOutline.skeletonModalOpen,
    title: "章节骨架生成中",
    message: detailedOutline.skeletonProgress?.message,
    progress: detailedOutline.skeletonProgress?.current ?? 0,
    onExpand: detailedOutline.openSkeletonModal,
    onCancel: detailedOutline.cancelSkeletonGenerate,
  };

  return {
    loading: outlineQuery.loading,
    dirty,
    showUnsavedGuard: dirty && outletActive,
    headerProps: {
      outlines,
      activeOutlineId,
      activeOutlineHasChapters: Boolean(outlines.find((outline) => outline.id === activeOutlineId)?.has_chapters),
      onSwitchOutline: (outlineId) => void switchOutline(outlineId),
      onOpenCreate: openCreateTitleModal,
      onOpenRename: openRenameTitleModal,
      onDelete: () => void deleteOutline(),
    },
    actionsBarProps: {
      dirty,
      saving,
      hasOutlineStructure: canCreateChapters,
      hasDetailedOutlines: detailedOutline.items.length > 0,
      onOpenGenerate: () => generation.setOpen(true),
      onOpenParse: parsing.openParseModal,
      onSave: () => void save(),
      onGoToDetailedTab: () => {
        /* handled by OutlinePage via setActiveTab */
      },
    },
    editorProps: {
      content,
      onChange: setContent,
    },
    titleModalProps: {
      open: titleModal.open,
      mode: titleModal.mode,
      title: titleModal.title,
      onTitleChange: (title) => setTitleModal((prev) => ({ ...prev, title })),
      onClose: closeTitleModal,
      onConfirm: () => void confirmTitleModal(),
    },
    generationModalProps: {
      open: generation.open,
      generating: generation.generating,
      genForm: generation.genForm,
      onGenFormChange: (patch) => generation.setGenForm((prev) => ({ ...prev, ...patch })),
      streamEnabled: generation.streamEnabled,
      onStreamEnabledChange: generation.setStreamEnabled,
      streamProgress: generation.streamProgress,
      streamPreviewJson: generation.streamPreviewJson,
      streamRawText: generation.streamRawText,
      preview: generation.genPreview,
      onClose: generation.closeModal,
      onCancelGenerate: generation.cancelGenerate,
      onGenerate: () => void generation.generate(),
      onClearPreview: generation.clearPreview,
      onOverwriteCurrent: () =>
        void (async () => {
          const shouldGenerateDetailed = (generation.genPreview?.chapters.length ?? 0) > 0;
          const applied = await generation.overwriteCurrentOutline();
          if (!applied || !shouldGenerateDetailed || !activeOutlineId) return;
          const generated = await detailedOutline.generate({});
          if (generated) {
            requestSwitchToDetailed(activeOutlineId);
          }
        })(),
      onSaveAsNew: () =>
        void (async () => {
          const shouldGenerateDetailed = (generation.genPreview?.chapters.length ?? 0) > 0;
          const createdOutline = await generation.saveAsNewOutline();
          if (!createdOutline || !shouldGenerateDetailed) return;
          const generated = await detailedOutline.generate({}, createdOutline.id);
          if (generated) {
            requestSwitchToDetailed(createdOutline.id);
          }
        })(),
      onPreviewContentChange: (next) =>
        generation.setGenPreview((prev) => (prev ? { ...prev, outline_md: next } : null)),
    },
    parsingModalProps: {
      open: parsing.open,
      parsing: parsing.parsing,
      parseForm: parsing.parseForm,
      parseProgress: parsing.parseProgress,
      parseResult: parsing.parseResult,
      agentCards: parsing.agentCards,
      activeTab: parsing.activeTab,
      onClose: parsing.closeParseModal,
      onCancelParse: parsing.cancelParse,
      onContentChange: parsing.handleContentChange,
      onFileUpload: (file) => void parsing.handleFileUpload(file),
      onAgentConfigChange: parsing.handleAgentConfigChange,
      onStartParse: () => void parsing.startParse(),
      onTabChange: parsing.setActiveTab,
      onApplyOutline: () =>
        void (async () => {
          const hasDetailed =
            Array.isArray(parsing.parseResult?.detailed_outlines) && parsing.parseResult.detailed_outlines.length > 0;
          const result = await parsing.applyOutline();
          const targetOutlineId = result.outlineId ?? activeOutlineId;
          if (!result.ok || !targetOutlineId) return;

          if (hasDetailed) {
            const appliedDetailed = await parsing.applyDetailedOutlines(targetOutlineId);
            if (!appliedDetailed) return;
            if (targetOutlineId === activeOutlineId) {
              await detailedOutline.refresh();
            }
            requestSwitchToDetailed(targetOutlineId);
            return;
          }

          const generated = await detailedOutline.generate({}, targetOutlineId);
          if (generated) {
            requestSwitchToDetailed(targetOutlineId);
          }
        })(),
      onApplyCharacters: () => void parsing.applyCharacters(),
      onApplyEntries: () => void parsing.applyEntries(),
      onApplyAll: () =>
        void (async () => {
          const hasDetailed =
            Array.isArray(parsing.parseResult?.detailed_outlines) && parsing.parseResult.detailed_outlines.length > 0;
          const result = await parsing.applyAll();
          const targetOutlineId = result.outlineId ?? activeOutlineId;
          if (!result.ok || !targetOutlineId) return;

          if (hasDetailed) {
            if (targetOutlineId === activeOutlineId) {
              await detailedOutline.refresh();
            }
            requestSwitchToDetailed(targetOutlineId);
            return;
          }

          const generated = await detailedOutline.generate({}, targetOutlineId);
          if (generated) {
            requestSwitchToDetailed(targetOutlineId);
          }
        })(),
    },
    detailedOutlineState: detailedOutline,
    outlineGenFloatingProps,
    parsingFloatingProps,
    detailedGenFloatingProps,
    skeletonGenFloatingProps,
    switchToDetailedRequested,
    clearSwitchToDetailedRequest,
    wizardBarProps: {
      projectId,
      currentStep: "outline",
      progress: wizard.progress,
      loading: wizard.loading,
      dirty,
      saving: saving || generation.generating || parsing.parsing,
      onSave: () => save(),
      primaryAction:
        wizard.progress.nextStep?.key === "chapters"
          ? detailedOutline.items.length > 0
            ? {
                label: "下一步：查看细纲并创建章节",
                disabled: generation.generating || parsing.parsing || saving,
                onClick: () => {
                  /* handled by OutlinePage */
                },
              }
            : canCreateChapters
              ? {
                  label: "下一步：生成细纲",
                  disabled: generation.generating || parsing.parsing || saving || detailedOutline.generating,
                  onClick: () => detailedOutline.openGenerateModal(),
                }
              : {
                  label: "下一步：先 AI 生成大纲",
                  disabled: generation.generating || parsing.parsing || saving,
                  onClick: () => generation.setOpen(true),
                }
          : undefined,
    },
  };
}
