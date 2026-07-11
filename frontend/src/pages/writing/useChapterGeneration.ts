import { toastApiError } from "../../lib/apiErrorPresentation";
import { toApiError } from "../../services/apiError";
import { useCallback, useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";

import type { GenerateForm } from "../../components/writing/types";
import type { ConfirmApi } from "../../components/ui/confirm";
import type { ToastApi } from "../../components/ui/toast";
import { UI_COPY } from "../../lib/uiCopy";
import { ApiError, apiJson } from "../../services/apiClient";
import { createChapterMarkerStreamParser } from "../../services/chapterMarkerStreamParser";
import { SSEError, SSEPostClient } from "../../services/sseClient";
import type { Chapter, ChapterListItem, LLMPreset } from "../../types";
import { extractMissingNumbers } from "./writingErrorUtils";
import {
  getWritingJumpToChapterLabel,
  getWritingMissingPrerequisiteMessage,
  WRITING_PAGE_COPY,
} from "./writingPageCopy";
import { appendMarkdown } from "./writingUtils";
import type { ChapterForm } from "./writingUtils";

type StreamProgress = {
  message: string;
  progress: number;
  status: string;
  charCount?: number;
};

type GenerateResponse = {
  content_md: string;
  summary: string;
  raw_output: string;
  dropped_params?: string[];
  generation_run_id?: string;
};

const DEFAULT_GEN_FORM: GenerateForm = {
  instruction: "",
  target_word_count: null,
  stream: true,
  style_id: null,
  memory_injection_enabled: true,
  previous_mode: "full",
  rag_enabled: true,
  context: {
    include_world_setting: true,
    include_style_guide: true,
    include_constraints: true,
    include_outline: true,
    include_smart_context: true,
    require_sequential: false,
    character_ids: [],
    entry_ids: [],
  },
};

export function useChapterGeneration(args: {
  projectId?: string;
  activeChapter: Chapter | null;
  chapters: ChapterListItem[];
  form: ChapterForm | null;
  setForm: Dispatch<SetStateAction<ChapterForm | null>>;
  preset: LLMPreset | null;
  dirty: boolean;
  saveChapter: () => Promise<boolean>;
  requestSelectChapter: (chapterId: string) => Promise<void>;
  toast: ToastApi;
  confirm: ConfirmApi;
}) {
  const {
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
  } = args;

  const [generating, setGenerating] = useState(false);
  const [genRequestId, setGenRequestId] = useState<string | null>(null);
  const [genStreamProgress, setGenStreamProgress] = useState<StreamProgress | null>(null);
  const genStreamClientRef = useRef<SSEPostClient | null>(null);
  const genStreamHasChunkRef = useRef(false);

  const [genForm, setGenForm] = useState<GenerateForm>(() => ({
    ...DEFAULT_GEN_FORM,
    context: { ...DEFAULT_GEN_FORM.context },
  }));

  const lastProjectIdRef = useRef<string | undefined>(undefined);

  useEffect(() => {
    if (lastProjectIdRef.current === projectId) return;
    lastProjectIdRef.current = projectId;
    setGenForm((prev) => ({
      ...prev,
      memory_injection_enabled: DEFAULT_GEN_FORM.memory_injection_enabled,
      previous_mode: DEFAULT_GEN_FORM.previous_mode,
      rag_enabled: DEFAULT_GEN_FORM.rag_enabled,
      context: {
        ...prev.context,
        character_ids: [],
        entry_ids: [],
      },
    }));
  }, [projectId]);

  const abortGenerate = useCallback(() => genStreamClientRef.current?.abort(), []);

  const generate = useCallback(
    async (
      mode: "replace" | "append",
      overrides?: { macro_seed?: string | null; prompt_override?: GenerateForm["prompt_override"] },
    ) => {
      if (!activeChapter || !form) return;
      if (!preset) {
        toast.toastError(WRITING_PAGE_COPY.promptPresetRequired);
        return;
      }
      const headers: Record<string, string> = { "X-LLM-Provider": preset.provider };
      const streamProviderSupported = preset.provider.startsWith("openai");

      if (dirty) {
        const choice = await confirm.choose(WRITING_PAGE_COPY.confirms.generateWithDirty);
        if (choice === "cancel") return;
        if (choice === "confirm") {
          const ok = await saveChapter();
          if (!ok) return;
        }
      }

      setGenerating(true);
      setGenRequestId(null);
      setGenStreamProgress(null);
      genStreamClientRef.current = null;
      genStreamHasChunkRef.current = false;
      try {
        const macroSeed =
          overrides && Object.prototype.hasOwnProperty.call(overrides, "macro_seed")
            ? overrides.macro_seed
            : genForm.macro_seed;
        const promptOverride =
          overrides && Object.prototype.hasOwnProperty.call(overrides, "prompt_override")
            ? overrides.prompt_override
            : genForm.prompt_override;

        const currentDraftTail = mode === "append" ? (form.content_md ?? "").trimEnd().slice(-1200) : null;
        const safeTargetWordCount =
          typeof genForm.target_word_count === "number" && genForm.target_word_count >= 100
            ? genForm.target_word_count
            : null;

        const payload = {
          mode,
          instruction: genForm.instruction,
          target_word_count: safeTargetWordCount,
          ...(typeof macroSeed === "string" && macroSeed.trim() ? { macro_seed: macroSeed.trim() } : {}),
          ...(promptOverride != null ? { prompt_override: promptOverride } : {}),
          style_id: genForm.style_id,
          memory_injection_enabled: genForm.memory_injection_enabled,
          memory_query_text: null,
          memory_modules: {
            story_memory: true,
            semantic_history: false,
            vector_rag: genForm.rag_enabled,
          },
          context: {
            include_world_setting: genForm.context.include_world_setting,
            include_style_guide: genForm.context.include_style_guide,
            include_constraints: genForm.context.include_constraints,
            include_outline: genForm.context.include_outline,
            include_smart_context: genForm.context.include_smart_context,
            require_sequential: genForm.context.require_sequential,
            character_ids: genForm.context.character_ids,
            entry_ids: genForm.context.entry_ids,
            previous_chapter: genForm.previous_mode === "full" ? "content" : "summary",
            current_draft_tail: currentDraftTail,
          },
        };

        const baseContent = form.content_md;
        const baseSummary = form.summary;

        const shouldStream = genForm.stream && streamProviderSupported;
        if (genForm.stream && !streamProviderSupported) {
          toast.toastWarning(WRITING_PAGE_COPY.generateUnsupportedProviderFallback);
        }

        if (shouldStream) {
          const parser = createChapterMarkerStreamParser();
          let parsedContent = "";
          let parsedSummary = "";
          const startContent =
            mode === "append"
              ? (() => {
                  const trimmed = (baseContent ?? "").trimEnd();
                  return trimmed ? `${trimmed}\n\n` : "";
                })()
              : "";
          let requestId: string | undefined;
          let nonFatalNoticed = false;
          let droppedParams: string[] = [];

          const processChunk = (chunk: string) => {
            const out = parser.push(chunk);
            if (out.contentDelta) parsedContent += out.contentDelta;
            if (out.summaryDelta) parsedSummary += out.summaryDelta;
            return out;
          };

          setForm((prev) => {
            if (!prev) return prev;
            return { ...prev, content_md: startContent, status: "drafting" };
          });

          const client = new SSEPostClient(`/api/chapters/${activeChapter.id}/generate-stream`, payload, {
            headers,
            onOpen: ({ requestId: rid }) => {
              requestId = rid;
              setGenRequestId(rid ?? null);
            },
            onProgress: ({ message, progress, status, charCount }) => {
              setGenStreamProgress({ message, progress, status, charCount });
              if (!nonFatalNoticed && status === "error") {
                nonFatalNoticed = true;
                toast.toastError(message, requestId);
              }
            },
            onChunk: (chunk) => {
              genStreamHasChunkRef.current = true;
              const out = processChunk(chunk);
              if (out.contentDelta) {
                setForm((prev) => {
                  if (!prev) return prev;
                  return { ...prev, content_md: (prev.content_md ?? "") + out.contentDelta, status: "drafting" };
                });
              }
            },
            onResult: (data) => {
              const obj = data && typeof data === "object" ? (data as Record<string, unknown>) : null;
              const content = typeof obj?.content_md === "string" ? obj.content_md : "";
              const summary = typeof obj?.summary === "string" ? obj.summary : "";
              const dropped = Array.isArray(obj?.dropped_params)
                ? obj.dropped_params.filter((p): p is string => typeof p === "string" && p.trim().length > 0)
                : [];
              droppedParams = dropped;
              const parseErrObj =
                obj?.parse_error && typeof obj.parse_error === "object"
                  ? (obj.parse_error as Record<string, unknown>)
                  : null;
              const parseErrCode = typeof parseErrObj?.code === "string" ? parseErrObj.code : undefined;
              const parseErrMessage = typeof parseErrObj?.message === "string" ? parseErrObj.message : undefined;
              if (parseErrCode === "OUTPUT_TRUNCATED") {
                toast.toastError(parseErrMessage ?? "输出被截断", requestId);
              }
              setForm((prev) => {
                if (!prev) return prev;
                const expectedContent = startContent + parsedContent;
                const nextContent = mode === "append" ? appendMarkdown(baseContent, content) : content;
                const nextSummaryRaw = summary || parsedSummary.trim();
                const shouldOverrideSummary = prev.summary === baseSummary;
                return {
                  ...prev,
                  content_md: prev.content_md === expectedContent ? nextContent : prev.content_md,
                  summary: shouldOverrideSummary ? nextSummaryRaw || prev.summary || baseSummary : prev.summary,
                  status: "drafting",
                };
              });
            },
          });
          genStreamClientRef.current = client;

          try {
            await client.connect();
            toast.toastSuccess(WRITING_PAGE_COPY.generateDoneUnsaved, requestId);
            if (!genStreamHasChunkRef.current) {
              toast.toastSuccess(WRITING_PAGE_COPY.generateEmptyStream, requestId);
            }
            if (droppedParams.length > 0) {
              toast.toastSuccess(`${UI_COPY.common.droppedParamsPrefix}${droppedParams.join("、")}`, requestId);
            }
          } catch (e) {
            const err = e as unknown;
            if (err instanceof SSEError && err.code === "ABORTED") {
              setForm((prev) => {
                if (!prev) return prev;
                const expectedContent = startContent + parsedContent;
                const expectedSummary = prev.summary === baseSummary;
                return {
                  ...prev,
                  content_md: prev.content_md === expectedContent ? baseContent : prev.content_md,
                  summary: expectedSummary ? baseSummary : prev.summary,
                };
              });
              toast.toastSuccess(WRITING_PAGE_COPY.generateCanceled, err.requestId ?? requestId);
              return;
            }
            if (err instanceof SSEError && err.code !== "SSE_SERVER_ERROR") {
              if (!genStreamHasChunkRef.current) {
                toast.toastError(WRITING_PAGE_COPY.generateFallback, err.requestId ?? requestId);
                const res = await apiJson<GenerateResponse>(`/api/chapters/${activeChapter.id}/generate`, {
                  method: "POST",
                  headers,
                  body: JSON.stringify(payload),
                });

                setForm((prev) => {
                  if (!prev) return prev;
                  const nextContent =
                    mode === "append"
                      ? appendMarkdown(prev.content_md, res.data.content_md ?? "")
                      : (res.data.content_md ?? "");
                  return {
                    ...prev,
                    content_md: nextContent,
                    summary: res.data.summary ?? prev.summary,
                    status: "drafting",
                  };
                });

                toast.toastSuccess(WRITING_PAGE_COPY.generateDoneUnsaved, res.request_id);
                const dp = res.data.dropped_params ?? [];
                if (dp.length > 0) {
                  toast.toastSuccess(`${UI_COPY.common.droppedParamsPrefix}${dp.join("、")}`, res.request_id);
                }
                return;
              }
              toastApiError(toast, err, { code: err.code, requestId: err.requestId ?? requestId });
              return;
            }
            if (err instanceof SSEError && err.code === "SSE_SERVER_ERROR") {
              toastApiError(toast, err, { code: err.code, requestId: err.requestId ?? requestId });
              return;
            }
            if (err instanceof ApiError) {
              const missingNumbers = extractMissingNumbers(err);
              if (missingNumbers.length > 0) {
                const targetNumber = missingNumbers[0]!;
                const target = chapters.find((c) => c.number === targetNumber);
                toast.toastError(
                  getWritingMissingPrerequisiteMessage(missingNumbers),
                  err.requestId,
                  target
                    ? {
                        label: getWritingJumpToChapterLabel(targetNumber),
                        onClick: () => void requestSelectChapter(target.id),
                      }
                    : undefined,
                );
                return;
              }
              toastApiError(toast, err);
              return;
            }
            toast.toastError(WRITING_PAGE_COPY.generateFailed);
          }
        } else {
          const res = await apiJson<GenerateResponse>(`/api/chapters/${activeChapter.id}/generate`, {
            method: "POST",
            headers,
            body: JSON.stringify(payload),
          });

          setForm((prev) => {
            if (!prev) return prev;
            const nextContent =
              mode === "append"
                ? appendMarkdown(prev.content_md, res.data.content_md ?? "")
                : (res.data.content_md ?? "");
            return {
              ...prev,
              content_md: nextContent,
              summary: res.data.summary ?? prev.summary,
              status: "drafting",
            };
          });

          toast.toastSuccess(WRITING_PAGE_COPY.generateDoneUnsaved, res.request_id);
          const dp = res.data.dropped_params ?? [];
          if (dp.length > 0) {
            toast.toastSuccess(`${UI_COPY.common.droppedParamsPrefix}${dp.join("、")}`, res.request_id);
          }
        }
      } catch (e) {
        const err = toApiError(e);
        const missingNumbers = extractMissingNumbers(err);
        if (missingNumbers.length > 0) {
          const targetNumber = missingNumbers[0]!;
          const target = chapters.find((c) => c.number === targetNumber);
          toast.toastError(
            getWritingMissingPrerequisiteMessage(missingNumbers),
            err.requestId,
            target
              ? {
                  label: getWritingJumpToChapterLabel(targetNumber),
                  onClick: () => void requestSelectChapter(target.id),
                }
              : undefined,
          );
          return;
        }
        toastApiError(toast, err);
      } finally {
        setGenerating(false);
      }
    },
    [activeChapter, chapters, confirm, dirty, form, genForm, preset, requestSelectChapter, saveChapter, setForm, toast],
  );

  return {
    generating,
    genRequestId,
    genStreamProgress,
    genStreamClientRef,
    genForm,
    setGenForm,
    generate,
    abortGenerate,
  };
}
