import { toastApiError } from "../../lib/apiErrorPresentation";
import { useCallback, useEffect, useRef, useState } from "react";

import type { ConfirmApi } from "../../components/ui/confirm";
import type { ToastApi } from "../../components/ui/toast";
import { ApiError, apiJson, sanitizeFilename } from "../../services/apiClient";
import { SSEError, SSEPostClient } from "../../services/sseClient";
import { batchCreateDetailedOutlines } from "../../services/detailedOutlinesApi";
import { createEntry } from "../../services/entriesApi";
import type { LLMPreset, Outline } from "../../types";

import { OUTLINE_PARSING_COPY } from "./outlineParsingCopy";
import {
  DEFAULT_PARSE_AGENT_CONFIG,
  DEFAULT_PARSE_FORM,
  INITIAL_AGENT_CARDS,
  createAgentCard,
  type AgentCardState,
  type OutlineParseAgentConfig,
  type OutlineParseForm,
  type OutlineParseProgress,
  type OutlineParseResult,
  type TaskPlanItem,
} from "./outlineParsingModels";

const STREAM_CONNECT_MAX_RETRIES = 2;
const STREAM_CONNECT_RETRY_BASE_DELAY_MS = 1200;

type SaveOutline = (
  nextContent?: string,
  nextStructure?: unknown,
  opts?: { silent?: boolean; snapshotContent?: string },
) => Promise<boolean>;

type CreateOutline = (title: string, contentMd: string, structure: unknown) => Promise<Outline | null>;
type ApplyOutlineResult = { ok: boolean; outlineId?: string };

type ParseTab = "outline" | "characters" | "entries";

function waitMs(ms: number): Promise<void> {
  return new Promise((resolve) => {
    globalThis.setTimeout(resolve, ms);
  });
}

async function fileToBase64(file: File): Promise<string> {
  return await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("file read failed"));
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error("file read failed"));
        return;
      }
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.readAsDataURL(file);
  });
}

function buildFreshParseForm(): OutlineParseForm {
  return {
    ...DEFAULT_PARSE_FORM,
    agent_config: { ...DEFAULT_PARSE_AGENT_CONFIG },
  };
}

function buildInitialAgentCards(): AgentCardState[] {
  return INITIAL_AGENT_CARDS.map((card) => ({ ...card, warnings: [...card.warnings] }));
}

function isCustomEventPayload(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Ensure an agent card exists in the array. If not found, create one dynamically.
 * Returns the (possibly updated) array.
 */
function ensureAgentCard(
  cards: AgentCardState[],
  agentId: string,
  displayName: string,
  agentType?: string,
): AgentCardState[] {
  if (cards.some((c) => c.id === agentId)) return cards;
  return [...cards, createAgentCard(agentId, displayName, agentType)];
}

export function useOutlineParsingState(args: {
  projectId?: string;
  outlineId?: string;
  preset: LLMPreset | null;
  dirty: boolean;
  save: SaveOutline;
  createOutline: CreateOutline;
  toast: ToastApi;
  confirm: ConfirmApi;
}) {
  const { projectId, outlineId, preset, dirty, save, createOutline, toast, confirm } = args;
  const [open, setOpen] = useState(false);
  const [parsing, setParsing] = useState(false);
  const [parseForm, setParseForm] = useState<OutlineParseForm>(() => buildFreshParseForm());
  const [parseProgress, setParseProgress] = useState<OutlineParseProgress | null>(null);
  const [parseResult, setParseResult] = useState<OutlineParseResult | null>(null);
  const [activeTab, setActiveTab] = useState<ParseTab>("outline");
  const [agentCards, setAgentCards] = useState<AgentCardState[]>([]);

  const streamClientRef = useRef<SSEPostClient | null>(null);
  const streamHasProgressRef = useRef(false);

  useEffect(() => {
    return () => {
      streamClientRef.current?.abort();
    };
  }, []);

  const openParseModal = useCallback(() => {
    setOpen(true);
  }, []);

  const closeParseModal = useCallback(() => {
    setOpen(false);
  }, []);

  const handleContentChange = useCallback((value: string) => {
    setParseForm((prev) => ({ ...prev, content: value }));
  }, []);

  const handleAgentConfigChange = useCallback((patch: Partial<OutlineParseAgentConfig>) => {
    setParseForm((prev) => ({ ...prev, agent_config: { ...prev.agent_config, ...patch } }));
  }, []);

  const handleFileUpload = useCallback(
    async (file: File | null) => {
      if (!file) {
        setParseForm((prev) => ({ ...prev, file_content: null, file_name: null }));
        return;
      }

      try {
        const contentBase64 = await fileToBase64(file);
        const safeName = sanitizeFilename(file.name) || "outline.txt";
        setParseForm((prev) => ({ ...prev, file_content: contentBase64, file_name: safeName }));
      } catch {
        toast.toastError("读取文件失败");
      }
    },
    [toast],
  );

  const cancelParse = useCallback(() => {
    streamClientRef.current?.abort();
  }, []);

  const startParse = useCallback(async () => {
    if (!projectId || !preset) return;

    const hasContent = Boolean((parseForm.content ?? "").trim());
    const hasFile = Boolean((parseForm.file_content ?? "").trim());
    if (!hasContent && !hasFile) {
      toast.toastError("请输入内容或上传文件");
      return;
    }

    setParsing(true);
    streamClientRef.current = null;
    streamHasProgressRef.current = false;
    setParseResult(null);
    setAgentCards(buildInitialAgentCards());
    setParseProgress({ message: "准备解析...", progress: 0, status: "processing" });

    try {
      const headers: Record<string, string> = { "X-LLM-Provider": preset.provider };
      const payload: OutlineParseForm = {
        content: parseForm.content,
        file_content: parseForm.file_content,
        file_name: parseForm.file_name,
        agent_config: parseForm.agent_config,
      };

      const isTransientStreamError = (error: unknown): error is SSEError =>
        error instanceof SSEError && error.code !== "SSE_SERVER_ERROR" && error.code !== "ABORTED";

      let streamResult: OutlineParseResult | null = null;
      let retryCount = 0;
      let done: { requestId?: string; result?: unknown; accumulatedContent: string } | null = null;

      while (retryCount <= STREAM_CONNECT_MAX_RETRIES) {
        const client = new SSEPostClient(`/api/projects/${projectId}/outline/parse-stream`, payload, {
          headers,
          onProgress: ({ message, progress, status }) => {
            streamHasProgressRef.current = true;
            setParseProgress({ message, progress, status });
          },
          onResult: (data) => {
            const typed = data as OutlineParseResult;
            streamResult = typed;
            setParseResult(typed);
            setActiveTab("outline");
          },
          onCustomEvent: (eventName, data) => {
            if (!isCustomEventPayload(data)) return;

            // Handle task_plan: create dynamic agent cards
            if (eventName === "task_plan") {
              const tasks = data.tasks;
              if (Array.isArray(tasks)) {
                setAgentCards((prev) => {
                  let updated = [...prev];
                  for (const task of tasks as TaskPlanItem[]) {
                    if (!updated.some((c) => c.id === task.id)) {
                      updated = [...updated, createAgentCard(task.id, task.display_name, task.type)];
                    }
                  }
                  // Add validation card at the end
                  if (!updated.some((c) => c.id === "validation")) {
                    updated = [...updated, createAgentCard("validation", "校验合并", "validation")];
                  }
                  return updated;
                });
              }
              return;
            }

            if (eventName === "agent_start") {
              const agentId = typeof data.agent === "string" ? data.agent : "";
              if (!agentId) return;
              const displayName =
                typeof data.display_name === "string" && data.display_name.trim() ? data.display_name : agentId;
              setAgentCards((prev) => {
                // Ensure card exists (may be a repair agent or dynamically added)
                const withCard = ensureAgentCard(prev, agentId, displayName);
                return withCard.map((card) =>
                  card.id === agentId
                    ? {
                        ...card,
                        displayName,
                        status: "running" as const,
                        streamingText: "",
                        durationMs: 0,
                        tokensUsed: 0,
                        warnings: [],
                        error: null,
                        retryCount: card.status === "pending" ? card.retryCount : card.retryCount + 1,
                      }
                    : card,
                );
              });
              return;
            }

            if (eventName === "agent_streaming") {
              const agentId = typeof data.agent === "string" ? data.agent : "";
              if (!agentId) return;
              const displayName =
                typeof data.display_name === "string" && data.display_name.trim() ? data.display_name : agentId;
              const text = typeof data.text === "string" ? data.text : "";
              setAgentCards((prev) =>
                prev.map((card) =>
                  card.id === agentId
                    ? {
                        ...card,
                        displayName,
                        status: card.status === "complete" ? ("complete" as const) : ("running" as const),
                        streamingText: text,
                      }
                    : card,
                ),
              );
              return;
            }

            if (eventName === "agent_complete") {
              const agentId = typeof data.agent === "string" ? data.agent : "";
              if (!agentId) return;
              const displayName =
                typeof data.display_name === "string" && data.display_name.trim() ? data.display_name : agentId;
              const finalStatus = data.status === "error" ? ("error" as const) : ("complete" as const);
              setAgentCards((prev) => {
                const withCard = ensureAgentCard(prev, agentId, displayName);
                return withCard.map((card) =>
                  card.id === agentId
                    ? {
                        ...card,
                        displayName,
                        status: finalStatus,
                        durationMs: typeof data.duration_ms === "number" ? data.duration_ms : 0,
                        tokensUsed: typeof data.tokens_used === "number" ? data.tokens_used : 0,
                        warnings: Array.isArray(data.warnings) ? data.warnings.map((item) => String(item)) : [],
                        error:
                          typeof data.error === "string" ? data.error : finalStatus === "error" ? "执行失败" : null,
                      }
                    : card,
                );
              });
            }
          },
        });
        streamClientRef.current = client;

        try {
          done = await client.connect();
          break;
        } catch (error) {
          if (
            isTransientStreamError(error) &&
            !streamHasProgressRef.current &&
            retryCount < STREAM_CONNECT_MAX_RETRIES
          ) {
            retryCount += 1;
            const delayMs = STREAM_CONNECT_RETRY_BASE_DELAY_MS * retryCount;
            setParseProgress((prev) => ({
              message: `连接中断，${Math.ceil(delayMs / 1000)} 秒后自动重连（${retryCount}/${STREAM_CONNECT_MAX_RETRIES}）...`,
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

      if (!streamResult && done.result) {
        const typed = done.result as OutlineParseResult;
        streamResult = typed;
        setParseResult(typed);
      }

      setParseProgress((prev) =>
        prev ? { ...prev, message: OUTLINE_PARSING_COPY.parseDone, progress: 100, status: "success" } : prev,
      );
      toast.toastSuccess(OUTLINE_PARSING_COPY.parseDone);
    } catch (error) {
      if (error instanceof SSEError && error.code === "ABORTED") {
        setParseProgress(null);
        toast.toastSuccess("已取消解析");
        return;
      }

      if (error instanceof SSEError) {
        setParseProgress((prev) => ({
          message: OUTLINE_PARSING_COPY.parseFailed,
          progress: prev?.progress ?? 0,
          status: "error",
        }));
        toastApiError(toast, error, { code: error.code, requestId: error.requestId });
        return;
      }

      if (error instanceof ApiError) {
        setParseProgress((prev) => ({
          message: OUTLINE_PARSING_COPY.parseFailed,
          progress: prev?.progress ?? 0,
          status: "error",
        }));
        toastApiError(toast, error);
        return;
      }

      setParseProgress((prev) => ({
        message: OUTLINE_PARSING_COPY.parseFailed,
        progress: prev?.progress ?? 0,
        status: "error",
      }));
      toast.toastError(OUTLINE_PARSING_COPY.parseFailed);
    } finally {
      streamClientRef.current = null;
      setParsing(false);
    }
  }, [parseForm, preset, projectId, toast]);

  const applyOutline = useCallback(async () => {
    if (!projectId || !parseResult) return { ok: false } satisfies ApplyOutlineResult;

    const volumes = Array.isArray(parseResult.outline?.volumes) ? parseResult.outline.volumes : [];
    const chapters = Array.isArray(parseResult.outline?.chapters) ? parseResult.outline.chapters : [];
    const outlineMd = String(parseResult.outline?.outline_md ?? "");

    if (!dirty) {
      const savedOk = await save(outlineMd, { volumes, chapters });
      return { ok: savedOk, outlineId };
    }

    const choice = await confirm.choose({
      title: "应用解析大纲？",
      description: "当前大纲有未保存修改。覆盖会替换当前大纲；另存会先保存当前大纲，再创建新大纲并切换（更安全）。",
      confirmText: "覆盖当前",
      secondaryText: "另存为新大纲",
      cancelText: "取消",
      danger: true,
    });
    if (choice === "cancel") return { ok: false, outlineId };
    if (choice === "confirm") {
      const savedOk = await save(outlineMd, { volumes, chapters });
      return { ok: savedOk, outlineId };
    }

    const savedOk = await save();
    if (!savedOk) return { ok: false, outlineId };
    const created = await createOutline("解析大纲", outlineMd, { volumes, chapters });
    if (!created) return { ok: false, outlineId };
    return { ok: true, outlineId: created.id };
  }, [confirm, createOutline, dirty, parseResult, projectId, save]);

  const applyCharacters = useCallback(async () => {
    if (!projectId || !parseResult) return false;
    const characters = Array.isArray(parseResult.characters) ? parseResult.characters : [];
    const payloads = characters
      .map((c) => ({
        name: String(c.name ?? "").trim(),
        role: String(c.role ?? "").trim() || null,
        profile: (c.profile ?? null) ? String(c.profile ?? "") : null,
        notes: (c.notes ?? null) ? String(c.notes ?? "") : null,
      }))
      .filter((c) => c.name);

    if (payloads.length === 0) return true;

    try {
      for (const payload of payloads) {
        await apiJson(`/api/projects/${projectId}/characters`, { method: "POST", body: JSON.stringify(payload) });
      }
      toast.toastSuccess(OUTLINE_PARSING_COPY.parseApplied);
      return true;
    } catch (error) {
      if (error instanceof ApiError) {
        toastApiError(toast, error);
      } else {
        toast.toastError("创建角色失败");
      }
      return false;
    }
  }, [parseResult, projectId, toast]);

  const applyEntries = useCallback(async () => {
    if (!projectId || !parseResult) return false;
    const entries = Array.isArray(parseResult.entries) ? parseResult.entries : [];
    const payloads = entries
      .map((e) => ({
        title: String(e.title ?? "").trim(),
        content: String(e.content ?? ""),
        tags: Array.isArray(e.tags) ? e.tags.map((t) => String(t)).filter(Boolean) : [],
      }))
      .filter((e) => e.title);

    if (payloads.length === 0) return true;

    try {
      for (const payload of payloads) {
        await createEntry(projectId, payload);
      }
      toast.toastSuccess(OUTLINE_PARSING_COPY.parseApplied);
      return true;
    } catch (error) {
      if (error instanceof ApiError) {
        toastApiError(toast, error);
      } else {
        toast.toastError("创建条目失败");
      }
      return false;
    }
  }, [parseResult, projectId, toast]);

  const applyDetailedOutlines = useCallback(
    async (targetOutlineId?: string) => {
      const effectiveOutlineId = targetOutlineId || outlineId;
      if (!projectId || !effectiveOutlineId || !parseResult) return false;
      const detailedOutlines = Array.isArray(parseResult.detailed_outlines) ? parseResult.detailed_outlines : [];
      if (detailedOutlines.length === 0) return true; // nothing to apply

      const items = detailedOutlines
        .map((d) => ({
          volume_number: typeof d.volume_number === "number" ? d.volume_number : 0,
          volume_title: String(d.volume_title ?? ""),
          volume_summary: String(d.volume_summary ?? ""),
          chapters: Array.isArray(d.chapters) ? d.chapters : [],
        }))
        .filter((d) => d.volume_number > 0);

      if (items.length === 0) return true;

      try {
        await batchCreateDetailedOutlines(projectId, effectiveOutlineId, items);
        return true;
      } catch (error) {
        if (error instanceof ApiError) {
          toastApiError(toast, error);
        } else {
          toast.toastError("保存细纲失败");
        }
        return false;
      }
    },
    [outlineId, parseResult, projectId, toast],
  );

  const applyAll = useCallback(async () => {
    const outlineApplied = await applyOutline();
    if (!outlineApplied.ok) return outlineApplied;

    const charactersApplied = await applyCharacters();
    if (!charactersApplied) return { ok: false, outlineId: outlineApplied.outlineId } satisfies ApplyOutlineResult;

    const entriesApplied = await applyEntries();
    if (!entriesApplied) return { ok: false, outlineId: outlineApplied.outlineId } satisfies ApplyOutlineResult;

    const detailedApplied = await applyDetailedOutlines(outlineApplied.outlineId);
    if (!detailedApplied) return { ok: false, outlineId: outlineApplied.outlineId } satisfies ApplyOutlineResult;

    return outlineApplied;
  }, [applyCharacters, applyDetailedOutlines, applyEntries, applyOutline]);

  return {
    open,
    parsing,
    parseForm,
    parseProgress,
    parseResult,
    agentCards,
    activeTab,
    setActiveTab,
    openParseModal,
    closeParseModal,
    cancelParse,
    handleContentChange,
    handleAgentConfigChange,
    handleFileUpload,
    startParse,
    applyOutline,
    applyCharacters,
    applyEntries,
    applyDetailedOutlines,
    applyAll,
  };
}
