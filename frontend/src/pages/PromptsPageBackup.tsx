import { WizardNextBar } from "../components/atelier/WizardNextBar";
import { LlmPresetPanelBackup } from "../components/prompts/LlmPresetPanelBackup";
import { UnsavedChangesGuard } from "../hooks/useUnsavedChangesGuard";
import { copyText } from "../lib/copyText";

import { PromptsVectorRagSection } from "./prompts/PromptsVectorRagSection";
import { usePromptsPageState } from "./prompts/usePromptsPageState";

function PromptsPageSkeleton() {
  return (
    <div className="grid gap-6 pb-24" aria-busy="true" aria-live="polite">
      <span className="sr-only">正在加载模型配置…</span>
      <div className="panel p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="grid gap-2">
            <div className="skeleton h-6 w-44" />
            <div className="skeleton h-4 w-72" />
          </div>
          <div className="skeleton h-9 w-40" />
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <div className="skeleton h-10 w-full" />
          <div className="skeleton h-10 w-full" />
          <div className="skeleton h-28 w-full sm:col-span-2" />
        </div>
      </div>
      <div className="panel p-6">
        <div className="skeleton h-5 w-40" />
        <div className="mt-3 grid gap-2">
          <div className="skeleton h-4 w-80" />
          <div className="skeleton h-4 w-72" />
        </div>
      </div>
    </div>
  );
}

function PromptsPageErrorState(props: { message: string; code: string; requestId?: string; onRetry: () => void }) {
  return (
    <div className="grid gap-6 pb-24">
      <div className="error-card">
        <div className="state-title">加载失败</div>
        <div className="state-desc">{`${props.message} (${props.code})`}</div>
        {props.requestId ? (
          <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-subtext">
            <span>request_id: {props.requestId}</span>
            <button
              className="btn btn-secondary btn-sm"
              onClick={() => void copyText(props.requestId!, { title: "复制 request_id" })}
              type="button"
            >
              复制 request_id
            </button>
          </div>
        ) : null}
        <div className="mt-4 flex flex-wrap gap-2">
          <button className="btn btn-primary" onClick={props.onRetry} type="button">
            重试
          </button>
        </div>
      </div>
    </div>
  );
}

export function PromptsPageBackup() {
  const state = usePromptsPageState();

  if (state.loading) return <PromptsPageSkeleton />;

  if (state.blockingLoadError) {
    return (
      <PromptsPageErrorState
        message={state.blockingLoadError.message}
        code={state.blockingLoadError.code}
        requestId={state.blockingLoadError.requestId}
        onRetry={() => void state.reloadAll()}
      />
    );
  }

  return (
    <div className="grid gap-6 pb-24">
      <div className="rounded-atelier border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
        备份页：用于和当前模型配置页做对照，不建议在这里做长期配置变更。
      </div>
      {state.dirty && state.outletActive ? <UnsavedChangesGuard when={state.dirty} /> : null}
      <LlmPresetPanelBackup {...state.llmPresetPanelProps} />
      <PromptsVectorRagSection {...state.vectorRagSectionProps} />

      <div className="surface p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-sm font-semibold">提示词工作室（beta）</div>
            <div className="text-xs text-subtext">提示词仅在「提示词工作室」中编辑/预览（与实际发送一致）。</div>
          </div>
          <button className="btn btn-secondary" onClick={state.goToPromptStudio} type="button">
            打开提示词工作室
          </button>
        </div>
      </div>

      <div className="text-xs text-subtext">快捷键：Ctrl/Cmd + S 保存（仅保存 LLM 配置）</div>

      <WizardNextBar {...state.wizardBarProps} />
    </div>
  );
}
