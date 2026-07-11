import { toastApiError } from "../../lib/apiErrorPresentation";
import { toApiError } from "../../services/apiError";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { useIsMobile } from "../../hooks/useIsMobile";
import { createRequestSeqGuard } from "../../lib/requestSeqGuard";
import { apiDownloadAttachment, apiJson } from "../../services/apiClient";
import { Drawer } from "../ui/Drawer";
import { useToast } from "../ui/toast";
import type { GenerationRun } from "./types";
import { formatWritingDisabledReason, WRITING_RUNTIME_COPY } from "./writingRuntimeCopy";

type Props = {
  open: boolean;
  onClose: () => void;
  loading: boolean;
  runs: GenerationRun[];
  selectedRun: GenerationRun | null;
  onSelectRun: (run: GenerationRun) => void;
};

export function GenerationHistoryDrawer(props: Props) {
  const { onClose, open } = props;
  const toast = useToast();
  const titleId = useId();
  const isMobile = useIsMobile();
  const [downloading, setDownloading] = useState(false);
  const [pipelineLoading, setPipelineLoading] = useState(false);
  const [pipelineRuns, setPipelineRuns] = useState<GenerationRun[]>([]);
  const [pipelineError, setPipelineError] = useState<{ code: string; message: string; requestId?: string } | null>(
    null,
  );
  const pipelineGuardRef = useRef(createRequestSeqGuard());

  const selectedRun = props.selectedRun;
  const paramsObj =
    selectedRun?.params && typeof selectedRun.params === "object"
      ? (selectedRun.params as Record<string, unknown>)
      : null;
  const memoryLogRaw = paramsObj?.memory_retrieval_log_json;
  const memoryLog = memoryLogRaw && typeof memoryLogRaw === "object" ? (memoryLogRaw as Record<string, unknown>) : null;
  const perSectionRaw = memoryLog?.per_section;
  const perSection =
    perSectionRaw && typeof perSectionRaw === "object" ? (perSectionRaw as Record<string, unknown>) : null;

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, open]);

  useEffect(() => {
    const guard = pipelineGuardRef.current;
    return () => guard.invalidate();
  }, []);

  const downloadDebugBundle = useCallback(async () => {
    if (!selectedRun) return;
    if (downloading) return;
    setDownloading(true);
    try {
      const { filename, blob, requestId } = await apiDownloadAttachment(
        `/api/generation_runs/${selectedRun.id}/debug_bundle`,
      );
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = filename || `debug_bundle_${selectedRun.id}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
      toast.toastSuccess("已下载排障包", requestId);
    } catch (e) {
      const err = toApiError(e);
      toastApiError(toast, err);
    } finally {
      setDownloading(false);
    }
  }, [downloading, selectedRun, toast]);

  useEffect(() => {
    if (!open) return;
    if (!selectedRun?.project_id || !selectedRun.request_id) {
      setPipelineRuns([]);
      setPipelineError(null);
      setPipelineLoading(false);
      return;
    }
    const seq = pipelineGuardRef.current.next();
    setPipelineLoading(true);
    setPipelineError(null);

    const qs = new URLSearchParams();
    qs.set("limit", "50");
    qs.set("request_id", selectedRun.request_id);
    if (selectedRun.chapter_id) qs.set("chapter_id", selectedRun.chapter_id);

    void apiJson<{ runs: GenerationRun[] }>(`/api/projects/${selectedRun.project_id}/generation_runs?${qs.toString()}`)
      .then((res) => {
        if (!pipelineGuardRef.current.isLatest(seq)) return;
        setPipelineRuns(res.data.runs ?? []);
      })
      .catch((e) => {
        if (!pipelineGuardRef.current.isLatest(seq)) return;
        const err = toApiError(e);
        setPipelineError({ code: err.code, message: err.message, requestId: err.requestId });
      })
      .finally(() => {
        if (pipelineGuardRef.current.isLatest(seq)) setPipelineLoading(false);
      });
  }, [open, selectedRun?.chapter_id, selectedRun?.project_id, selectedRun?.request_id]);

  const pipelineSteps = useMemo(() => {
    const sorted = [...pipelineRuns].sort(
      (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
    );
    return sorted.map((r) => {
      const type = String(r.type ?? "");
      const stage =
        type === "plan_chapter"
          ? "plan"
          : type === "chapter" || type === "chapter_stream"
            ? "generate"
            : type === "post_edit" || type === "post_edit_sanitize"
              ? "post_edit"
              : type || "unknown";
      return { run: r, stage };
    });
  }, [pipelineRuns]);

  return (
    <Drawer
      open={open}
      onClose={onClose}
      ariaLabelledBy={titleId}
      side={isMobile ? "bottom" : "right"}
      panelClassName="h-[85dvh] sm:h-full w-full sm:max-w-2xl border-t sm:border-t-0 sm:border-l border-border bg-canvas p-4 sm:p-6 shadow-sm"
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-content text-2xl text-ink" id={titleId}>
            生成记录
          </div>
          <div className="mt-1 text-xs text-subtext">最近 5 条</div>
        </div>
        <button className="btn btn-secondary" aria-label="关闭" onClick={onClose} type="button">
          关闭
        </button>
      </div>

      <div className="mt-5 grid gap-4">
        {props.loading ? <div className="text-sm text-subtext">加载中...</div> : null}

        <div className="grid gap-3 md:grid-cols-2">
          <div className="rounded-atelier border border-border bg-surface p-2">
            {props.runs.length === 0 ? (
              <div className="p-3 text-sm text-subtext">暂无生成记录。</div>
            ) : (
              <div className="flex flex-col gap-1">
                {props.runs.map((r) => {
                  const active = props.selectedRun?.id === r.id;
                  const failed = Boolean(r.error);
                  return (
                    <button
                      key={r.id}
                      className={
                        active
                          ? "ui-focus-ring ui-transition-fast rounded-atelier bg-canvas px-3 py-2 text-left text-sm text-ink"
                          : "ui-focus-ring ui-transition-fast rounded-atelier px-3 py-2 text-left text-sm text-subtext hover:bg-canvas hover:text-ink"
                      }
                      onClick={() => props.onSelectRun(r)}
                      type="button"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <div className="min-w-0 truncate">
                          <span className="mr-2 text-xs text-subtext">{new Date(r.created_at).toLocaleString()}</span>
                          <span className="truncate">{r.type}</span>
                        </div>
                        <span className="shrink-0 text-[11px] text-subtext">{failed ? "failed" : "ok"}</span>
                      </div>
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          <div className="rounded-atelier border border-border bg-surface p-4">
            {!selectedRun ? (
              <div className="text-sm text-subtext">选择一条记录查看详情。</div>
            ) : (
              <div className="grid gap-3">
                <div className="text-sm text-ink">{selectedRun.type}</div>
                <div className="text-xs text-subtext">
                  {selectedRun.provider ?? "unknown"} / {selectedRun.model ?? "unknown"}
                </div>
                <div className="flex items-center gap-2 text-xs text-subtext">
                  <span className="truncate">run_id: {selectedRun.id}</span>
                  <button
                    className="btn btn-ghost px-2 py-1 text-xs"
                    onClick={async () => {
                      await navigator.clipboard.writeText(selectedRun.id ?? "");
                    }}
                    type="button"
                  >
                    复制
                  </button>
                </div>
                {selectedRun.request_id ? (
                  <div className="flex items-center gap-2 text-xs text-subtext">
                    <span className="truncate">request_id: {selectedRun.request_id}</span>
                    <button
                      className="btn btn-ghost px-2 py-1 text-xs"
                      onClick={async () => {
                        await navigator.clipboard.writeText(selectedRun.request_id ?? "");
                      }}
                      type="button"
                    >
                      复制
                    </button>
                  </div>
                ) : null}
                <div>
                  <button
                    className="btn btn-secondary"
                    disabled={downloading}
                    onClick={() => void downloadDebugBundle()}
                    type="button"
                  >
                    {downloading ? "下载中..." : "下载排障包"}
                  </button>
                  <div className="mt-2 rounded-atelier border border-border bg-canvas p-3 text-[11px] text-subtext">
                    <div className="text-xs text-ink">排障包说明</div>
                    <ul className="mt-1 list-disc pl-5">
                      <li>用途：定位生成失败、提示词渲染、记忆检索注入等问题</li>
                      <li>{WRITING_RUNTIME_COPY.bundlePrivacyHint}</li>
                      <li>{WRITING_RUNTIME_COPY.bundleSafetyHint}</li>
                    </ul>
                  </div>
                </div>

                <details open>
                  <summary className="ui-transition-fast cursor-pointer text-xs text-subtext hover:text-ink">
                    流水线视图（按 request_id 串联）
                  </summary>
                  <div className="mt-2 grid gap-2">
                    {pipelineLoading ? <div className="text-xs text-subtext">加载中...</div> : null}
                    {pipelineError ? (
                      <div className="text-xs text-danger">
                        {pipelineError.code}: {pipelineError.message}
                        {pipelineError.requestId ? (
                          <span className="ml-2">request_id: {pipelineError.requestId}</span>
                        ) : null}
                      </div>
                    ) : null}
                    {pipelineSteps.length === 0 && !pipelineLoading ? (
                      <div className="text-xs text-subtext">
                        暂无可串联的流水线 runs（该 run 可能缺少 request_id）。
                      </div>
                    ) : (
                      <div className="grid gap-2">
                        {pipelineSteps.map(({ run, stage }) => {
                          const active = run.id === selectedRun.id;
                          const failed = Boolean(run.error);
                          return (
                            <button
                              key={run.id}
                              aria-label={`pipeline run_id: ${String(run.id ?? "")} ${failed ? "failed" : "ok"}`}
                              className={
                                active
                                  ? "ui-focus-ring ui-transition-fast rounded-atelier border border-accent/40 bg-accent/10 px-3 py-2 text-left text-xs text-ink"
                                  : "ui-focus-ring ui-transition-fast rounded-atelier border border-border bg-canvas px-3 py-2 text-left text-xs text-subtext hover:bg-surface hover:text-ink"
                              }
                              onClick={() => props.onSelectRun(run)}
                              type="button"
                            >
                              <div className="flex items-center justify-between gap-2">
                                <div className="min-w-0 truncate">
                                  <span className="mr-2 font-mono">{stage}</span>
                                  <span className="mr-2 font-mono">{String(run.type ?? "")}</span>
                                  <span className="truncate font-mono">run_id: {String(run.id ?? "")}</span>
                                </div>
                                <span className="shrink-0 font-mono text-[11px] text-subtext">
                                  {failed ? "failed" : "ok"}
                                </span>
                              </div>
                              {run.request_id ? (
                                <div className="mt-1 truncate font-mono text-[11px] text-subtext">
                                  request_id: {String(run.request_id ?? "")}
                                </div>
                              ) : null}
                            </button>
                          );
                        })}
                      </div>
                    )}
                  </div>
                </details>

                {memoryLog ? (
                  <details open>
                    <summary className="ui-transition-fast cursor-pointer text-xs text-subtext hover:text-ink">
                      memory_retrieval_log_json
                    </summary>
                    <div className="mt-2 grid gap-2 text-xs text-subtext">
                      <div>
                        enabled: {String(memoryLog.enabled ?? "")} | phase: {String(memoryLog.phase ?? "")}
                      </div>
                      <div className="truncate">query_text: {String(memoryLog.query_text ?? "")}</div>
                      {Array.isArray(memoryLog.errors) && memoryLog.errors.length ? (
                        <div className="text-warning">errors: {memoryLog.errors.join(", ")}</div>
                      ) : null}
                    </div>

                    {perSection ? (
                      <div className="mt-3 grid gap-2">
                        {Object.entries(perSection)
                          .sort(([a], [b]) => a.localeCompare(b))
                          .map(([section, raw]) => {
                            const o = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
                            const enabled = Boolean(o.enabled);
                            const disabledReason = typeof o.disabled_reason === "string" ? o.disabled_reason : null;
                            return (
                              <div key={section} className="rounded-atelier border border-border bg-canvas p-2">
                                <div className="flex items-center justify-between gap-2 text-xs">
                                  <span className="font-mono text-ink">{section}</span>
                                  {enabled ? (
                                    <span className="text-success">enabled</span>
                                  ) : (
                                    <span className="text-warning">{formatWritingDisabledReason(disabledReason)}</span>
                                  )}
                                </div>
                              </div>
                            );
                          })}
                      </div>
                    ) : (
                      <pre className="mt-2 max-h-40 overflow-auto rounded-atelier border border-border bg-canvas p-3 text-xs text-ink">
                        {JSON.stringify(memoryLog, null, 2)}
                      </pre>
                    )}
                  </details>
                ) : null}

                <details open>
                  <summary className="ui-transition-fast cursor-pointer text-xs text-subtext hover:text-ink">
                    params
                  </summary>
                  <pre className="mt-2 max-h-40 overflow-auto rounded-atelier border border-border bg-canvas p-3 text-xs text-ink">
                    {JSON.stringify(selectedRun.params ?? {}, null, 2)}
                  </pre>
                </details>
                <details>
                  <summary className="ui-transition-fast cursor-pointer text-xs text-subtext hover:text-ink">
                    prompt_system
                  </summary>
                  <pre className="mt-2 max-h-40 overflow-auto rounded-atelier border border-border bg-canvas p-3 text-xs text-ink">
                    {selectedRun.prompt_system ?? ""}
                  </pre>
                </details>
                <details>
                  <summary className="ui-transition-fast cursor-pointer text-xs text-subtext hover:text-ink">
                    prompt_user
                  </summary>
                  <pre className="mt-2 max-h-40 overflow-auto rounded-atelier border border-border bg-canvas p-3 text-xs text-ink">
                    {selectedRun.prompt_user ?? ""}
                  </pre>
                </details>
                <details open>
                  <summary className="ui-transition-fast cursor-pointer text-xs text-subtext hover:text-ink">
                    output / error
                  </summary>
                  <pre className="mt-2 max-h-56 overflow-auto rounded-atelier border border-border bg-canvas p-3 text-xs text-ink">
                    {selectedRun.error ? JSON.stringify(selectedRun.error, null, 2) : (selectedRun.output_text ?? "")}
                  </pre>
                </details>
              </div>
            )}
          </div>
        </div>
      </div>
    </Drawer>
  );
}
