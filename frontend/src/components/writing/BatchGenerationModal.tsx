import { useId } from "react";

import { Modal } from "../ui/Modal";
import { ProgressBar } from "../ui/ProgressBar";

import type { BatchGenerationTask, BatchGenerationTaskItem } from "./types";

function tryExtractRequestId(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (parsed && typeof parsed === "object") {
      const obj = parsed as Record<string, unknown>;
      const direct = obj.request_id ?? obj.requestId;
      if (typeof direct === "string" && direct.trim()) return direct;

      const nestedError = obj.error;
      if (nestedError && typeof nestedError === "object") {
        const err = nestedError as Record<string, unknown>;
        const nested = err.request_id ?? err.requestId;
        if (typeof nested === "string" && nested.trim()) return nested;
      }
    }
  } catch {
    // noop
  }

  const match = raw.match(/request[_-]?id\\s*[:=]\\s*([A-Za-z0-9_-]+)/i);
  return match?.[1] ?? null;
}

export function BatchGenerationModal(props: {
  open: boolean;
  batchLoading: boolean;
  activeChapterNumber: number | null;
  batchCount: number;
  setBatchCount: (value: number) => void;
  batchIncludeExisting: boolean;
  setBatchIncludeExisting: (value: boolean) => void;
  batchTask: BatchGenerationTask | null;
  batchItems: BatchGenerationTaskItem[];
  onClose: () => void;
  onCancelTask: () => void;
  onStartTask: () => void;
  onApplyItemToEditor: (item: BatchGenerationTaskItem) => void;
}) {
  const titleId = useId();
  const task = props.batchTask;
  const taskRunning = task && (task.status === "queued" || task.status === "running");
  const taskFailed = task?.status === "failed";
  const requestId = taskFailed ? tryExtractRequestId(task?.error_json) : null;
  const taskProgressPercent = task
    ? Math.round((task.total_count > 0 ? task.completed_count / task.total_count : 0) * 100)
    : 0;
  return (
    <Modal
      open={props.open}
      onClose={props.batchLoading ? undefined : props.onClose}
      panelClassName="surface max-w-2xl p-5"
      ariaLabelledBy={titleId}
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-content text-xl text-ink" id={titleId}>
            批量生成
          </div>
          <div className="mt-1 text-xs text-subtext">
            批量生成只会写入“生成记录”，不会自动保存到章节；你可以逐章“应用到编辑器”后再保存。
          </div>
        </div>
        <button
          className="btn btn-secondary"
          aria-label="关闭"
          onClick={props.onClose}
          disabled={props.batchLoading}
          type="button"
        >
          关闭
        </button>
      </div>

      <div className="mt-4 grid gap-3">
        <div className="grid gap-2 rounded-atelier border border-border bg-canvas p-3">
          <div className="text-sm font-medium text-ink">步骤 1：选择范围</div>
          <div className="text-xs text-subtext">
            起点：{props.activeChapterNumber ? `第 ${props.activeChapterNumber} 章之后` : "从第 1 章开始"}
          </div>
          <div className="text-[11px] text-subtext">范围由当前选中章节决定；生成会从起点之后顺序推进。</div>
        </div>

        <div className="grid gap-2 rounded-atelier border border-border bg-canvas p-3">
          <div className="text-sm font-medium text-ink">步骤 2：参数</div>
          <div className="flex flex-wrap items-end gap-3">
            <label className="grid gap-1">
              <span className="text-xs text-subtext">数量（1~20）</span>
              <input
                className="input w-28"
                min={1}
                max={20}
                type="number"
                value={props.batchCount}
                disabled={props.batchLoading || Boolean(taskRunning)}
                onChange={(e) => props.setBatchCount(Math.max(1, Math.min(20, Number(e.target.value) || 1)))}
              />
            </label>
            <label className="flex items-center gap-2 pb-2 text-sm text-ink">
              <input
                className="checkbox"
                type="checkbox"
                checked={props.batchIncludeExisting}
                disabled={props.batchLoading || Boolean(taskRunning)}
                onChange={(e) => props.setBatchIncludeExisting(e.target.checked)}
              />
              包含已有内容章节
            </label>
          </div>
        </div>

        <div className="grid gap-3 rounded-atelier border border-border bg-surface p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-medium text-ink">步骤 3：执行与结果</div>
            <div className="flex items-center gap-2">
              {taskRunning ? (
                <button
                  className="btn btn-secondary"
                  disabled={props.batchLoading}
                  onClick={props.onCancelTask}
                  type="button"
                >
                  {props.batchLoading ? "取消中..." : "取消任务"}
                </button>
              ) : (
                <button
                  className="btn btn-primary"
                  disabled={props.batchLoading}
                  onClick={props.onStartTask}
                  type="button"
                >
                  {props.batchLoading ? "启动中..." : "开始批量生成"}
                </button>
              )}
            </div>
          </div>

          {!task ? <div className="text-sm text-subtext">尚未开始：请先设置参数，然后点击“开始批量生成”。</div> : null}

          {task ? (
            <>
              <div className="text-sm text-ink">
                任务状态：{task.status}（{task.completed_count}/{task.total_count}）
              </div>
              <ProgressBar ariaLabel="批量生成任务进度" value={taskProgressPercent} />

              {taskFailed && task.error_json ? (
                <div className="rounded-atelier border border-border bg-canvas p-3">
                  <div className="text-sm text-ink">失败原因</div>
                  <div className="mt-1 break-words text-xs text-subtext">{task.error_json}</div>
                  {requestId ? (
                    <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
                      <div className="text-xs text-subtext">request_id: {requestId}</div>
                      <button
                        className="btn btn-ghost px-2 py-1 text-xs"
                        onClick={() => void navigator.clipboard.writeText(requestId)}
                        type="button"
                      >
                        复制 request_id
                      </button>
                    </div>
                  ) : null}
                  <div className="mt-2 text-[11px] text-subtext">
                    建议：检查缺少前置章节内容提示；或稍后重试；必要时把 request_id 发给管理员排查。
                  </div>
                </div>
              ) : null}

              <div className="max-h-64 overflow-auto rounded-atelier border border-border bg-canvas">
                {props.batchItems.length === 0 ? (
                  <div className="p-3 text-sm text-subtext">暂无任务项</div>
                ) : (
                  <div className="divide-y divide-border">
                    {props.batchItems.map((it) => (
                      <div key={it.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
                        <div className="min-w-0">
                          <div className="text-sm text-ink">第 {it.chapter_number} 章</div>
                          <div className="text-xs text-subtext">
                            {it.status}
                            {it.error_message ? ` · ${it.error_message}` : ""}
                          </div>
                        </div>
                        <div className="flex items-center gap-2">
                          {it.status === "succeeded" && it.chapter_id && it.generation_run_id ? (
                            <button
                              className="btn btn-secondary"
                              onClick={() => props.onApplyItemToEditor(it)}
                              disabled={props.batchLoading}
                              type="button"
                            >
                              应用到编辑器
                            </button>
                          ) : null}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </>
          ) : null}
        </div>
      </div>
    </Modal>
  );
}
