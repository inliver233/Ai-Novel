import { toastApiError } from "../../lib/apiErrorPresentation";
import { toApiError } from "../../services/apiError";
import { useCallback, useEffect, useRef, useState } from "react";

import type { ConfirmApi } from "../../components/ui/confirm";
import type { ToastApi } from "../../components/ui/toast";
import { ApiError, apiJson } from "../../services/apiClient";
import { SSEError, SSEPostClient } from "../../services/sseClient";
import type { LLMPreset, Outline } from "../../types";
import { normalizeOutlineGenResult, parseOutlineGenResultFromText, type OutlineGenResult } from "../outlineParsing";

import { getOutlineStreamRetryMessage, OUTLINE_COPY } from "./outlineCopy";
import {
  appendCappedRawText,
  buildGeneratedOutlineTitle,
  DEFAULT_OUTLINE_GEN_FORM,
  STREAM_RAW_MAX_CHARS,
  toFinalPreviewJson,
  waitMs,
  type OutlineGenForm,
  type OutlineStreamProgress,
} from "./outlineModels";

const STREAM_CONNECT_MAX_RETRIES = 2;
const STREAM_CONNECT_RETRY_BASE_DELAY_MS = 1200;

type SaveOutline = (
  nextContent?: string,
  nextStructure?: unknown,
  opts?: { silent?: boolean; snapshotContent?: string },
) => Promise<boolean>;

type CreateOutline = (title: string, contentMd: string, structure: unknown) => Promise<Outline | null>;

export function useOutlineGenerationState(args: {
  projectId?: string;
  preset: LLMPreset | null;
  dirty: boolean;
  save: SaveOutline;
  createOutline: CreateOutline;
  confirm: ConfirmApi;
  toast: ToastApi;
}) {
  const { projectId, preset, dirty, save, createOutline, confirm, toast } = args;
  const [open, setOpen] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [genPreview, setGenPreview] = useState<OutlineGenResult | null>(null);
  const [genForm, setGenForm] = useState<OutlineGenForm>(DEFAULT_OUTLINE_GEN_FORM);
  const [streamEnabled, setStreamEnabled] = useState(false);
  const [streamProgress, setStreamProgress] = useState<OutlineStreamProgress | null>(null);
  const [streamRawText, setStreamRawText] = useState("");
  const [streamPreviewJson, setStreamPreviewJson] = useState("");
  const streamClientRef = useRef<SSEPostClient | null>(null);
  const streamHasChunkRef = useRef(false);

  useEffect(() => {
    return () => {
      streamClientRef.current?.abort();
    };
  }, []);

  const closeModal = useCallback(() => {
    setOpen(false);
  }, []);

  const cancelGenerate = useCallback(() => {
    streamClientRef.current?.abort();
  }, []);

  const clearPreview = useCallback(() => {
    setGenPreview(null);
  }, []);

  const overwriteCurrentOutline = useCallback(async () => {
    if (!genPreview) return false;
    const preview = genPreview;
    const ok = !dirty ? true : await confirm.confirm({ ...OUTLINE_COPY.confirms.overwriteDirty, danger: true });
    if (!ok) return false;
    const savedOk = await save(preview.outline_md, { volumes: preview.volumes, chapters: preview.chapters });
    if (!savedOk) return false;
    setOpen(false);
    setGenPreview(null);
    return true;
  }, [confirm, dirty, genPreview, save]);

  const saveAsNewOutline = useCallback(async () => {
    if (!projectId || !genPreview) return null;
    const preview = genPreview;

    if (dirty) {
      const choice = await confirm.choose(OUTLINE_COPY.confirms.saveAsNewDirty);
      if (choice === "cancel") return null;
      if (choice === "confirm") {
        const ok = await save();
        if (!ok) return null;
      }
    }

    const created = await createOutline(buildGeneratedOutlineTitle(), preview.outline_md, {
      volumes: preview.volumes,
      chapters: preview.chapters,
    });
    if (!created) return null;
    setOpen(false);
    setGenPreview(null);
    return created;
  }, [confirm, createOutline, dirty, genPreview, projectId, save]);

  const generate = useCallback(async () => {
    if (!projectId || !preset) return;
    setGenerating(true);
    streamClientRef.current = null;
    streamHasChunkRef.current = false;
    setGenPreview(null);
    setStreamRawText("");
    setStreamPreviewJson("");
    setStreamProgress(null);

    try {
      const headers: Record<string, string> = { "X-LLM-Provider": preset.provider };
      const payload = {
        requirements: {
          chapter_count: genForm.chapter_count,
          tone: genForm.tone,
          pacing: genForm.pacing,
        },
        context: {
          include_world_setting: genForm.include_world_setting,
          include_characters: genForm.include_characters,
        },
      };

      if (!streamEnabled) {
        const response = await apiJson<OutlineGenResult>(`/api/projects/${projectId}/outline/generate`, {
          method: "POST",
          headers,
          body: JSON.stringify(payload),
        });
        setGenPreview(response.data);
        toast.toastSuccess(OUTLINE_COPY.generateDone);
        return;
      }

      setStreamProgress({ message: "开始生成...", progress: 0, status: "processing" });
      let streamRaw = "";
      let streamResult: OutlineGenResult | null = null;
      let retryCount = 0;

      const applyStreamResult = (candidate: unknown, fallbackRaw = ""): boolean => {
        const normalized = normalizeOutlineGenResult(candidate, fallbackRaw);
        if (!normalized) return false;
        streamResult = normalized;
        setGenPreview(normalized);
        setStreamPreviewJson(toFinalPreviewJson(normalized));
        return true;
      };

      const isTransientStreamError = (error: unknown): error is SSEError =>
        error instanceof SSEError && error.code !== "SSE_SERVER_ERROR" && error.code !== "ABORTED";

      try {
        let done: { requestId?: string; result?: unknown; accumulatedContent: string } | null = null;
        while (retryCount <= STREAM_CONNECT_MAX_RETRIES) {
          const client = new SSEPostClient(`/api/projects/${projectId}/outline/generate-stream`, payload, {
            headers,
            onProgress: ({ message, progress, status }) => {
              setStreamProgress({ message, progress, status });
            },
            onChunk: (content) => {
              streamHasChunkRef.current = true;
              streamRaw += content;
              setStreamRawText((prev) => appendCappedRawText(prev, content, STREAM_RAW_MAX_CHARS));
            },
            onResult: (data) => {
              void applyStreamResult(data, streamRaw);
            },
          });
          streamClientRef.current = client;
          try {
            done = await client.connect();
            break;
          } catch (error) {
            if (
              isTransientStreamError(error) &&
              !streamHasChunkRef.current &&
              retryCount < STREAM_CONNECT_MAX_RETRIES
            ) {
              retryCount += 1;
              const delayMs = STREAM_CONNECT_RETRY_BASE_DELAY_MS * retryCount;
              setStreamProgress((prev) => ({
                message: getOutlineStreamRetryMessage(delayMs, retryCount, STREAM_CONNECT_MAX_RETRIES),
                progress: prev?.progress ?? 0,
                status: "processing",
              }));
              await waitMs(delayMs);
              continue;
            }
            throw error;
          }
        }

        if (!done) {
          throw new SSEError({ code: "SSE_STREAM_ERROR", message: "流式重连后仍失败" });
        }
        if (!streamResult) {
          const doneApplied = applyStreamResult(done.result, done.accumulatedContent || streamRaw);
          if (!doneApplied) {
            const parsedFromRaw = parseOutlineGenResultFromText(done.accumulatedContent || streamRaw);
            if (parsedFromRaw) {
              streamResult = parsedFromRaw;
              setGenPreview(parsedFromRaw);
              setStreamPreviewJson(toFinalPreviewJson(parsedFromRaw));
            }
          }
        }
        if (!streamResult) {
          setStreamProgress((prev) => ({
            message: "生成已结束，但结果解析失败，请重试",
            progress: prev?.progress ?? 100,
            status: "error",
          }));
          toast.toastError(OUTLINE_COPY.generateParseFailed);
          return;
        }
        setStreamProgress((prev) => (prev ? { ...prev, message: "完成", progress: 100, status: "success" } : prev));
        toast.toastSuccess(OUTLINE_COPY.generateDone);
      } catch (error) {
        if (error instanceof SSEError && error.code !== "SSE_SERVER_ERROR" && error.code !== "ABORTED") {
          if (!streamHasChunkRef.current) {
            setStreamProgress({ message: "流式失败，回退非流式...", progress: 0, status: "processing" });
            toast.toastError(OUTLINE_COPY.generateFallback);
            const response = await apiJson<OutlineGenResult>(`/api/projects/${projectId}/outline/generate`, {
              method: "POST",
              headers,
              body: JSON.stringify(payload),
            });
            const normalized = normalizeOutlineGenResult(response.data, "");
            setGenPreview(normalized ?? response.data);
            if (normalized) {
              setStreamPreviewJson(toFinalPreviewJson(normalized));
            }
            setStreamProgress(null);
            toast.toastSuccess(OUTLINE_COPY.generateDone);
          } else {
            setStreamProgress((prev) => ({
              message: "流式连接中断，可重试生成",
              progress: prev?.progress ?? 0,
              status: "error",
            }));
            toastApiError(toast, error, { code: error.code, requestId: error.requestId });
          }
          return;
        }

        if (error instanceof SSEError && error.code === "SSE_SERVER_ERROR") {
          setStreamProgress((prev) => ({
            message: "生成失败，可重试生成",
            progress: prev?.progress ?? 0,
            status: "error",
          }));
          toastApiError(toast, error, { code: error.code, requestId: error.requestId });
          return;
        }

        if (error instanceof SSEError && error.code === "ABORTED") {
          setStreamProgress(null);
          toast.toastSuccess(OUTLINE_COPY.generateCanceled);
          return;
        }

        if (error instanceof ApiError) {
          setStreamProgress((prev) => ({
            message: "生成失败，可重试生成",
            progress: prev?.progress ?? 0,
            status: "error",
          }));
          toastApiError(toast, error);
          return;
        }

        setStreamProgress((prev) => ({
          message: "生成失败，可重试生成",
          progress: prev?.progress ?? 0,
          status: "error",
        }));
        toast.toastError("流式生成失败");
      }
    } catch (error) {
      const err = toApiError(error);
      toastApiError(toast, err);
    } finally {
      streamClientRef.current = null;
      setGenerating(false);
    }
  }, [genForm, preset, projectId, streamEnabled, toast]);

  return {
    open,
    setOpen,
    closeModal,
    generating,
    genPreview,
    setGenPreview,
    genForm,
    setGenForm,
    streamEnabled,
    setStreamEnabled,
    streamProgress,
    streamRawText,
    streamPreviewJson,
    generate,
    cancelGenerate,
    clearPreview,
    overwriteCurrentOutline,
    saveAsNewOutline,
  };
}
