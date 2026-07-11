import { toastApiError } from "../../lib/apiErrorPresentation";
import { toApiError } from "../../services/apiError";
import { useCallback, useEffect, useRef, useState } from "react";

import { useConfirm } from "../../components/ui/confirm";
import { useToast } from "../../components/ui/toast";
import { ApiError } from "../../services/apiClient";
import {
  type ChapterSkeletonGenerateRequest,
  listDetailedOutlines,
  getDetailedOutline,
  updateDetailedOutline,
  deleteDetailedOutline,
  createChaptersFromDetailedOutline,
  type DetailedOutlineListItem,
  type DetailedOutline,
  type DetailedOutlineGenerateRequest,
} from "../../services/detailedOutlinesApi";
import { chapterStore } from "../../services/chapterStore";
import { SSEError, SSEPostClient } from "../../services/sseClient";

import { OUTLINE_COPY } from "./outlineCopy";
import { appendCappedRawText, STREAM_RAW_MAX_CHARS } from "./outlineModels";

export type DetailedOutlineProgress = {
  current: number;
  total: number;
  message: string;
};

export type DetailedOutlineState = {
  items: DetailedOutlineListItem[];
  selected: DetailedOutline | null;
  generating: boolean;
  progress: DetailedOutlineProgress | null;
  skeletonGenerating: boolean;
  skeletonProgress: DetailedOutlineProgress | null;
  skeletonStreamRawText: string;
  skeletonStreamResult: Record<string, unknown> | null;
  editing: boolean;
  editContent: string;
  editTitle: string;
  saving: boolean;
  generateModalOpen: boolean;
  skeletonModalOpen: boolean;
  refresh: () => Promise<void>;
  selectVolume: (id: string) => Promise<void>;
  deselectVolume: () => void;
  openGenerateModal: () => void;
  closeGenerateModal: () => void;
  generate: (request: DetailedOutlineGenerateRequest, targetOutlineId?: string) => Promise<boolean>;
  cancelGenerate: () => void;
  openSkeletonModal: () => void;
  closeSkeletonModal: () => void;
  cancelSkeletonGenerate: () => void;
  generateChapterSkeleton: (detailedOutlineId: string, request: ChapterSkeletonGenerateRequest) => Promise<void>;
  startEdit: () => void;
  cancelEdit: () => void;
  setEditContent: (value: string) => void;
  setEditTitle: (value: string) => void;
  saveEdit: () => Promise<void>;
  deleteVolume: (id: string) => Promise<void>;
  createChapters: (id: string) => Promise<void>;
};

function isRecordLike(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function useDetailedOutlineState(
  projectId: string | undefined,
  outlineId: string | undefined,
): DetailedOutlineState {
  const toast = useToast();
  const confirm = useConfirm();

  const [items, setItems] = useState<DetailedOutlineListItem[]>([]);
  const [selected, setSelected] = useState<DetailedOutline | null>(null);
  const [generating, setGenerating] = useState(false);
  const [progress, setProgress] = useState<DetailedOutlineProgress | null>(null);
  const [skeletonGenerating, setSkeletonGenerating] = useState(false);
  const [skeletonProgress, setSkeletonProgress] = useState<DetailedOutlineProgress | null>(null);
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState("");
  const [editTitle, setEditTitle] = useState("");
  const [saving, setSaving] = useState(false);
  const [generateModalOpen, setGenerateModalOpen] = useState(false);
  const [skeletonModalOpen, setSkeletonModalOpen] = useState(false);
  const [skeletonStreamRawText, setSkeletonStreamRawText] = useState("");
  const [skeletonStreamResult, setSkeletonStreamResult] = useState<Record<string, unknown> | null>(null);

  const streamClientRef = useRef<SSEPostClient | null>(null);
  const skeletonStreamRef = useRef<SSEPostClient | null>(null);

  useEffect(() => {
    return () => {
      streamClientRef.current?.abort();
      skeletonStreamRef.current?.abort();
    };
  }, []);

  const refresh = useCallback(async () => {
    if (!projectId || !outlineId) {
      setItems([]);
      return;
    }
    try {
      const list = await listDetailedOutlines(projectId, outlineId);
      setItems(list);
    } catch (error) {
      const err = toApiError(error);
      toastApiError(toast, err);
    }
  }, [projectId, outlineId, toast]);

  useEffect(() => {
    if (projectId && outlineId) {
      void refresh();
    } else {
      setItems([]);
      setSelected(null);
    }
  }, [projectId, outlineId, refresh]);

  const selectVolume = useCallback(
    async (id: string) => {
      try {
        const detail = await getDetailedOutline(id);
        setSelected(detail);
        setEditing(false);
      } catch (error) {
        const err = toApiError(error);
        toastApiError(toast, err);
      }
    },
    [toast],
  );

  const deselectVolume = useCallback(() => {
    setSelected(null);
    setEditing(false);
  }, []);

  const openGenerateModal = useCallback(() => {
    setGenerateModalOpen(true);
  }, []);

  const closeGenerateModal = useCallback(() => {
    setGenerateModalOpen(false);
  }, []);

  const cancelGenerate = useCallback(() => {
    streamClientRef.current?.abort();
  }, []);

  const openSkeletonModal = useCallback(() => {
    setSkeletonModalOpen(true);
  }, []);

  const closeSkeletonModal = useCallback(() => {
    setSkeletonModalOpen(false);
  }, []);

  const cancelSkeletonGenerate = useCallback(() => {
    skeletonStreamRef.current?.abort();
  }, []);

  const generate = useCallback(
    async (request: DetailedOutlineGenerateRequest, targetOutlineId?: string) => {
      const effectiveOutlineId = targetOutlineId || outlineId;
      if (!projectId || !effectiveOutlineId) return false;
      setGenerating(true);
      setProgress({ current: 0, total: 0, message: "..." });
      streamClientRef.current = null;

      try {
        const url = `/api/projects/${projectId}/outlines/${effectiveOutlineId}/detailed_outlines/generate`;
        const client = new SSEPostClient(url, request, {
          onProgress: ({ message, progress: pct }) => {
            setProgress((prev) => ({
              current: prev?.current ?? 0,
              total: prev?.total ?? 0,
              message: message || `${Math.round(pct)}%`,
            }));
          },
          onCustomEvent: (eventName, data) => {
            const obj = data as Record<string, unknown> | null;
            if (eventName === "start") {
              const total = typeof obj?.total_volumes === "number" ? obj.total_volumes : 0;
              setProgress((prev) => ({
                current: prev?.current ?? 0,
                total,
                message: prev?.message ?? "...",
              }));
            } else if (eventName === "volume_start") {
              const volNum = typeof obj?.volume_number === "number" ? obj.volume_number : 0;
              const volTitle = typeof obj?.volume_title === "string" ? obj.volume_title : "";
              const total =
                typeof obj?.total_volumes === "number"
                  ? obj.total_volumes
                  : typeof obj?.total === "number"
                    ? obj.total
                    : 0;
              setProgress({
                current: volNum,
                total,
                message: `${OUTLINE_COPY.detailedOutline.generatingVolumePrefix}${volNum}${OUTLINE_COPY.detailedOutline.volumeSuffix}/${OUTLINE_COPY.detailedOutline.totalPrefix}${total}${OUTLINE_COPY.detailedOutline.volumeSuffix}: ${volTitle}`,
              });
            } else if (eventName === "volume_complete") {
              const volNum = typeof obj?.volume_number === "number" ? obj.volume_number : 0;
              const total =
                typeof obj?.total_volumes === "number"
                  ? obj.total_volumes
                  : typeof obj?.total === "number"
                    ? obj.total
                    : 0;
              setProgress((prev) => ({
                current: volNum,
                total,
                message: prev?.message ?? "",
              }));
            }
          },
          onDone: () => {
            setProgress((prev) =>
              prev ? { ...prev, message: OUTLINE_COPY.detailedOutline.generateDetailedDone } : prev,
            );
          },
        });
        streamClientRef.current = client;

        await client.connect();
        if (effectiveOutlineId === outlineId) {
          await refresh();
        }
        toast.toastSuccess(OUTLINE_COPY.detailedOutline.generateDetailedDone);
        return true;
      } catch (error) {
        if (error instanceof SSEError && error.code === "ABORTED") {
          toast.toastSuccess(OUTLINE_COPY.detailedOutline.generateCanceled);
          if (effectiveOutlineId === outlineId) {
            await refresh();
          }
          return false;
        }
        if (error instanceof SSEError || error instanceof ApiError) {
          toastApiError(toast, error, { code: error.code, requestId: error.requestId });
        } else {
          toast.toastError(OUTLINE_COPY.detailedOutline.generateDetailedFailed);
        }
        return false;
      } finally {
        streamClientRef.current = null;
        setGenerating(false);
      }
    },
    [projectId, outlineId, refresh, toast],
  );

  const startEdit = useCallback(() => {
    if (!selected) return;
    setEditContent(selected.content_md ?? "");
    setEditTitle(selected.volume_title);
    setEditing(true);
  }, [selected]);

  const generateChapterSkeleton = useCallback(
    async (detailedOutlineId: string, request: ChapterSkeletonGenerateRequest) => {
      setSkeletonGenerating(true);
      setSkeletonStreamRawText("");
      setSkeletonStreamResult(null);
      setSkeletonProgress({ current: 0, total: 100, message: "..." });
      skeletonStreamRef.current = null;

      try {
        const url = `/api/detailed_outlines/${detailedOutlineId}/generate_chapters_stream`;
        const client = new SSEPostClient(url, request, {
          onProgress: ({ message, progress: pct }) => {
            setSkeletonProgress(() => ({
              current: Math.round(pct),
              total: 100,
              message: message || `${Math.round(pct)}%`,
            }));
          },
          onChunk: (content: string) => {
            setSkeletonStreamRawText((prev) => appendCappedRawText(prev, content, STREAM_RAW_MAX_CHARS));
          },
          onResult: (data: unknown) => {
            if (isRecordLike(data)) {
              setSkeletonStreamResult(data);
            }
          },
          onDone: () => {
            setSkeletonProgress((prev) =>
              prev ? { ...prev, message: OUTLINE_COPY.detailedOutline.generateSkeletonDone } : prev,
            );
          },
        });
        skeletonStreamRef.current = client;

        await client.connect();
        await refresh();
        if (projectId) chapterStore.invalidateProjectChapters(projectId);
        // 刷新当前选中的细纲详情
        try {
          const updated = await getDetailedOutline(detailedOutlineId);
          setSelected(updated);
        } catch {
          // ignore — list already refreshed
        }
        toast.toastSuccess(OUTLINE_COPY.detailedOutline.generateSkeletonDone);
      } catch (error) {
        if (error instanceof SSEError && error.code === "ABORTED") {
          toast.toastSuccess(OUTLINE_COPY.detailedOutline.generateSkeletonCanceled);
          await refresh();
          if (projectId) chapterStore.invalidateProjectChapters(projectId);
          return;
        }
        if (error instanceof SSEError || error instanceof ApiError) {
          toastApiError(toast, error, { code: error.code, requestId: error.requestId });
        } else {
          toast.toastError(OUTLINE_COPY.detailedOutline.generateSkeletonFailed);
        }
      } finally {
        skeletonStreamRef.current = null;
        setSkeletonGenerating(false);
      }
    },
    [projectId, refresh, toast],
  );

  const cancelEdit = useCallback(() => {
    setEditing(false);
  }, []);

  const saveEdit = useCallback(async () => {
    if (!selected) return;
    setSaving(true);
    try {
      const updated = await updateDetailedOutline(selected.id, {
        volume_title: editTitle,
        content_md: editContent,
      });
      setSelected(updated);
      setEditing(false);
      await refresh();
      toast.toastSuccess(OUTLINE_COPY.detailedOutline.saveDetailedSuccess);
    } catch (error) {
      const err = toApiError(error);
      toastApiError(toast, err);
    } finally {
      setSaving(false);
    }
  }, [editContent, editTitle, refresh, selected, toast]);

  const deleteVolumeHandler = useCallback(
    async (id: string) => {
      const ok = await confirm.confirm({
        ...OUTLINE_COPY.detailedOutline.deleteDetailedConfirm,
        danger: true,
      });
      if (!ok) return;
      try {
        await deleteDetailedOutline(id);
        if (selected?.id === id) {
          setSelected(null);
          setEditing(false);
        }
        await refresh();
        toast.toastSuccess(OUTLINE_COPY.detailedOutline.deletedSuccess);
      } catch (error) {
        const err = toApiError(error);
        toastApiError(toast, err);
      }
    },
    [confirm, refresh, selected?.id, toast],
  );

  const createChapters = useCallback(
    async (id: string) => {
      const ok = await confirm.confirm({
        title: OUTLINE_COPY.detailedOutline.createChaptersFromDetailed,
        description: OUTLINE_COPY.detailedOutline.createChaptersFromDetailedHint,
        confirmText: OUTLINE_COPY.confirm,
      });
      if (!ok) return;
      try {
        const result = await createChaptersFromDetailedOutline(id);
        toast.toastSuccess(
          `${OUTLINE_COPY.detailedOutline.createdChaptersPrefix}${result.count}${OUTLINE_COPY.detailedOutline.chapterCountSuffix}`,
        );
        await refresh();
        if (projectId) chapterStore.invalidateProjectChapters(projectId);
      } catch (error) {
        const err = toApiError(error);
        if (err.code === "CONFLICT" && err.status === 409) {
          const replaceOk = await confirm.confirm({
            title: OUTLINE_COPY.detailedOutline.replaceChaptersTitle,
            description: err.message || OUTLINE_COPY.detailedOutline.replaceChaptersDescription,
            confirmText: OUTLINE_COPY.detailedOutline.replaceChaptersConfirmText,
            danger: true,
          });
          if (!replaceOk) return;
          try {
            const retryResult = await createChaptersFromDetailedOutline(id, true);
            toast.toastSuccess(
              `${OUTLINE_COPY.detailedOutline.replacedChaptersPrefix}${retryResult.count}${OUTLINE_COPY.detailedOutline.chapterCountSuffix}`,
            );
            await refresh();
            if (projectId) chapterStore.invalidateProjectChapters(projectId);
          } catch (retryError) {
            const retryErr = toApiError(retryError);
            toastApiError(toast, retryErr);
          }
          return;
        }
        toastApiError(toast, err);
      }
    },
    [confirm, projectId, refresh, toast],
  );

  return {
    items,
    selected,
    generating,
    progress,
    skeletonGenerating,
    skeletonProgress,
    skeletonStreamRawText,
    skeletonStreamResult,
    editing,
    editContent,
    editTitle,
    saving,
    generateModalOpen,
    skeletonModalOpen,
    refresh,
    selectVolume,
    deselectVolume,
    openGenerateModal,
    closeGenerateModal,
    generate,
    cancelGenerate,
    openSkeletonModal,
    closeSkeletonModal,
    cancelSkeletonGenerate,
    generateChapterSkeleton,
    startEdit,
    cancelEdit,
    setEditContent,
    setEditTitle,
    saveEdit,
    deleteVolume: deleteVolumeHandler,
    createChapters,
  };
}
