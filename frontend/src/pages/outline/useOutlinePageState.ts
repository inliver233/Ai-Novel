import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ComponentProps } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { WizardNextBar } from "../../components/atelier/WizardNextBar";
import { useConfirm } from "../../components/ui/confirm";
import { useToast } from "../../components/ui/toast";
import { useProjectData } from "../../hooks/useProjectData";
import { useAutoSave } from "../../hooks/useAutoSave";
import { usePersistentOutletIsActive } from "../../hooks/usePersistentOutlet";
import { useSaveHotkey } from "../../hooks/useSaveHotkey";
import { useWizardProgress } from "../../hooks/useWizardProgress";
import { ApiError, apiJson } from "../../services/apiClient";
import { chapterStore } from "../../services/chapterStore";
import { importAllStoryMemories } from "../../services/storyMemoryApi";
import { markWizardProjectChanged } from "../../services/wizard";
import type { LLMPreset, Outline, OutlineListItem, Project } from "../../types";
import {
  buildChapterPlanFromChapter,
  deriveOutlineFromStoredContent,
  parseOutlineMarkdownChapters,
  type OutlineGenChapter,
} from "../outlineParsing";

import type {
  OutlineActionsBarProps,
  OutlineEditorSectionProps,
  OutlineGenerationModalProps,
  OutlineHeaderSectionProps,
  OutlineTitleModalProps,
} from "./OutlinePageSections";
import { getOutlineCreateChaptersDescription, getOutlineCreatedChaptersText, OUTLINE_COPY } from "./outlineCopy";
import { buildNextOutlineTitle } from "./outlineModels";
import { useOutlineGenerationState } from "./useOutlineGenerationState";

type OutlineLoaded = {
  outlines: OutlineListItem[];
  outline: Outline;
  preset: LLMPreset;
};

const OUTLINE_PROMISE_SOURCE = "outline_import_v1";

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
  wizardBarProps: ComponentProps<typeof WizardNextBar>;
};

export function useOutlinePageState(): OutlinePageState {
  const { projectId } = useParams();
  const toast = useToast();
  const confirm = useConfirm();
  const navigate = useNavigate();
  const outletActive = usePersistentOutletIsActive();
  const wizard = useWizardProgress(projectId);
  const refreshWizard = wizard.refresh;
  const bumpWizardLocal = wizard.bumpLocal;

  const [saving, setSaving] = useState(false);
  const [importing, setImporting] = useState(false);
  const [outlines, setOutlines] = useState<OutlineListItem[]>([]);
  const [activeOutline, setActiveOutline] = useState<Outline | null>(null);
  const [preset, setPreset] = useState<LLMPreset | null>(null);
  const [baseline, setBaseline] = useState("");
  const [content, setContent] = useState("");
  const [titleModal, setTitleModal] = useState<{ open: boolean; mode: "create" | "rename"; title: string }>({
    open: false,
    mode: "create",
    title: "",
  });

  const wizardRefreshTimerRef = useRef<number | null>(null);
  const savingRef = useRef(false);
  const queuedSaveRef = useRef<{
    nextContent?: string;
    nextStructure?: unknown;
    opts?: { silent?: boolean; snapshotContent?: string };
  } | null>(null);
  const queuedSavePromiseRef = useRef<Promise<boolean> | null>(null);
  const queuedSaveResolveRef = useRef<((ok: boolean) => void) | null>(null);

  const outlineQuery = useProjectData<OutlineLoaded>(projectId, async (id) => {
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
  });

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
        normalizedStored.chapters.length > 0
          ? { chapters: normalizedStored.chapters }
          : outlineQuery.data.outline.structure,
    });
    setPreset(outlineQuery.data.preset);
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

  const save = useCallback<SaveOutline>(
    async (nextContent, nextStructure, opts) => {
      if (!projectId) return false;
      if (savingRef.current) {
        queuedSaveRef.current = { nextContent, nextStructure, opts };
        if (!queuedSavePromiseRef.current) {
          queuedSavePromiseRef.current = new Promise<boolean>((resolve) => {
            queuedSaveResolveRef.current = resolve;
          });
        }
        return queuedSavePromiseRef.current;
      }

      const silent = Boolean(opts?.silent);
      const snapshotContent = opts?.snapshotContent;
      const toSave = snapshotContent ?? nextContent ?? content;
      if (
        nextContent === undefined &&
        snapshotContent === undefined &&
        nextStructure === undefined &&
        toSave === baseline
      ) {
        return true;
      }

      savingRef.current = true;
      setSaving(true);
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
      } finally {
        setSaving(false);
        savingRef.current = false;
        if (queuedSaveRef.current) {
          const queued = queuedSaveRef.current;
          queuedSaveRef.current = null;
          void save(queued.nextContent, queued.nextStructure, queued.opts).then((queuedOk) => {
            if (queuedSaveResolveRef.current) {
              queuedSaveResolveRef.current(queuedOk);
              queuedSaveResolveRef.current = null;
            }
            queuedSavePromiseRef.current = null;
          });
        } else if (queuedSaveResolveRef.current) {
          queuedSaveResolveRef.current(true);
          queuedSaveResolveRef.current = null;
          queuedSavePromiseRef.current = null;
        }
      }
    },
    [baseline, bumpWizardLocal, content, projectId, refreshWizard, toast],
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

  const createOutline = useCallback(
    async (title: string, contentMd: string, structure: unknown) => {
      if (!projectId) return;
      try {
        await apiJson<{ outline: Outline }>(`/api/projects/${projectId}/outlines`, {
          method: "POST",
          body: JSON.stringify({ title, content_md: contentMd, structure }),
        });
        markWizardProjectChanged(projectId);
        bumpWizardLocal();
        await refreshOutline();
        await refreshWizard();
        toast.toastSuccess(OUTLINE_COPY.createdAndSwitched);
      } catch (error) {
        const err = error as ApiError;
        toast.toastError(`${err.message} (${err.code})`, err.requestId);
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

  const syncOutlinePromiseStoryMemories = useCallback(
    async (chapters: OutlineGenChapter[], createdChapters: Array<{ id: string; number: number }>) => {
      if (!projectId) return;
      const chapterIdByNumber = new Map(createdChapters.map((chapter) => [chapter.number, chapter.id]));
      const memories = chapters.flatMap((chapter) =>
        (chapter.promise_items ?? []).map((item) => {
          const contentParts = [item.content];
          if (item.resolution_hint) contentParts.push(`回收：${item.resolution_hint}`);
          return {
            chapter_id: chapterIdByNumber.get(chapter.number) ?? null,
            memory_type: "foreshadow",
            title: item.title || null,
            content: contentParts.filter(Boolean).join("\n"),
            importance_score: item.source_kind === "hook" ? 0.7 : item.source_kind === "both" ? 0.8 : 0.9,
            story_timeline: chapter.number,
            is_foreshadow: 1,
            metadata: {
              source_kind: item.source_kind,
              planted_chapter_number: item.planted_chapter_number,
              planned_chapter_numbers: item.planned_chapter_numbers,
              resolution_hint: item.resolution_hint,
              chapter_title: chapter.title,
            },
          };
        }),
      );
      await importAllStoryMemories(projectId, {
        source_tag: OUTLINE_PROMISE_SOURCE,
        replace_existing: true,
        memories,
      });
    },
    [projectId],
  );

  const createChaptersFromOutline = useCallback(async () => {
    if (!projectId || chaptersForSkeleton.length === 0) return;

    const ok = await confirm.confirm({
      ...OUTLINE_COPY.confirms.createSkeleton,
      description: getOutlineCreateChaptersDescription(chaptersForSkeleton.length),
    });
    if (!ok) return;

    const payload = {
      chapters: chaptersForSkeleton.map((chapter) => ({
        number: chapter.number,
        title: chapter.title,
        plan: buildChapterPlanFromChapter(chapter),
      })),
    };

    try {
      const createdChapters = await chapterStore.bulkCreateProjectChapters(projectId, payload);
      await syncOutlinePromiseStoryMemories(chaptersForSkeleton, createdChapters);
      toast.toastSuccess(getOutlineCreatedChaptersText(chaptersForSkeleton.length));
      markWizardProjectChanged(projectId);
      bumpWizardLocal();
      navigate(`/projects/${projectId}/writing`);
    } catch (error) {
      const err = error as ApiError;
      if (err.code === "CONFLICT" && err.status === 409) {
        const replaceOk = await confirm.confirm({ ...OUTLINE_COPY.confirms.replaceSkeleton, danger: true });
        if (!replaceOk) return;
        try {
          const createdChapters = await chapterStore.bulkCreateProjectChapters(projectId, payload, { replace: true });
          await syncOutlinePromiseStoryMemories(chaptersForSkeleton, createdChapters);
          toast.toastSuccess(getOutlineCreatedChaptersText(chaptersForSkeleton.length, true));
          markWizardProjectChanged(projectId);
          bumpWizardLocal();
          navigate(`/projects/${projectId}/writing`);
        } catch (retryError) {
          const retryErr = retryError as ApiError;
          toast.toastError(`${retryErr.message} (${retryErr.code})`, retryErr.requestId);
        }
        return;
      }
      toast.toastError(`${err.message} (${err.code})`, err.requestId);
    }
  }, [bumpWizardLocal, chaptersForSkeleton, confirm, navigate, projectId, syncOutlinePromiseStoryMemories, toast]);

  const importOutlineFile = useCallback(
    async (file: File) => {
      if (!projectId) return;
      const confirmed = await confirm.confirm(OUTLINE_COPY.confirms.importOutline);
      if (!confirmed) return;

      setImporting(true);
      try {
        const raw = await file.text();
        const parsed = deriveOutlineFromStoredContent(raw, null);
        const chapters = parsed.chapters.length > 0 ? parsed.chapters : parseOutlineMarkdownChapters(raw);
        if (chapters.length === 0) {
          toast.toastError(OUTLINE_COPY.importNoChapters);
          return;
        }

        const saved = await save(parsed.normalizedContentMd, { chapters });
        if (!saved) return;
        toast.toastSuccess(OUTLINE_COPY.importSaved);

        const payload = {
          chapters: chapters.map((chapter) => ({
            number: chapter.number,
            title: chapter.title,
            plan: buildChapterPlanFromChapter(chapter),
          })),
        };

        try {
          const createdChapters = await chapterStore.bulkCreateProjectChapters(projectId, payload);
          await syncOutlinePromiseStoryMemories(chapters, createdChapters);
          toast.toastSuccess(getOutlineCreatedChaptersText(chapters.length));
          markWizardProjectChanged(projectId);
          bumpWizardLocal();
          navigate(`/projects/${projectId}/writing`);
        } catch (error) {
          const err = error as ApiError;
          if (err.code === "CONFLICT" && err.status === 409) {
            const replaceOk = await confirm.confirm({ ...OUTLINE_COPY.confirms.replaceSkeleton, danger: true });
            if (!replaceOk) return;
            const createdChapters = await chapterStore.bulkCreateProjectChapters(projectId, payload, { replace: true });
            await syncOutlinePromiseStoryMemories(chapters, createdChapters);
            toast.toastSuccess(getOutlineCreatedChaptersText(chapters.length, true));
            markWizardProjectChanged(projectId);
            bumpWizardLocal();
            navigate(`/projects/${projectId}/writing`);
            return;
          }
          toast.toastError(`${err.message} (${err.code})`, err.requestId);
        }
      } catch {
        toast.toastError(OUTLINE_COPY.importReadFailed);
      } finally {
        setImporting(false);
      }
    },
    [bumpWizardLocal, confirm, navigate, projectId, save, syncOutlinePromiseStoryMemories, toast],
  );

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
      canCreateChapters,
      createChaptersDisabledReason: canCreateChapters ? undefined : OUTLINE_COPY.createChaptersDisabledReason,
      dirty,
      saving,
      importing,
      onCreateChapters: () => void createChaptersFromOutline(),
      onImportOutlineFile: (file) => void importOutlineFile(file),
      onOpenGenerate: () => generation.setOpen(true),
      onSave: () => void save(),
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
      onOverwriteCurrent: () => void generation.overwriteCurrentOutline(),
      onSaveAsNew: () => void generation.saveAsNewOutline(),
      onPreviewContentChange: (next) =>
        generation.setGenPreview((prev) => (prev ? { ...prev, outline_md: next } : null)),
    },
    wizardBarProps: {
      projectId,
      currentStep: "outline",
      progress: wizard.progress,
      loading: wizard.loading,
      dirty,
      saving: saving || generation.generating,
      onSave: () => save(),
      primaryAction:
        wizard.progress.nextStep?.key === "chapters"
          ? canCreateChapters
            ? {
                label: "下一步：创建章节骨架",
                disabled: generation.generating || saving,
                onClick: createChaptersFromOutline,
              }
            : {
                label: "下一步：先 AI 生成大纲",
                disabled: generation.generating || saving,
                onClick: () => generation.setOpen(true),
              }
          : undefined,
    },
  };
}
