import { QueryErrorCard } from "../components/atelier/QueryErrorCard";
import { WizardNextBar } from "../components/atelier/WizardNextBar";
import { UnsavedChangesGuard } from "../hooks/useUnsavedChangesGuard";
import { copyText } from "../lib/copyText";
import { formatApiErrorFields } from "../lib/apiErrorPresentation";

import { SettingsCoreSections } from "./settings/SettingsCoreSections";
import { useSettingsPageState } from "./settings/useSettingsPageState";

function SettingsPageSkeleton() {
  return (
    <div className="grid gap-6 pb-24" aria-busy="true" aria-live="polite">
      <span className="sr-only">正在加载设置…</span>
      <section className="panel p-6">
        <div className="flex items-start justify-between gap-4">
          <div className="grid gap-2">
            <div className="skeleton h-6 w-32" />
            <div className="skeleton h-4 w-56" />
          </div>
          <div className="skeleton h-9 w-40" />
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-3">
          <div className="grid gap-1 sm:col-span-1">
            <div className="skeleton h-4 w-16" />
            <div className="skeleton h-10 w-full" />
          </div>
          <div className="grid gap-1 sm:col-span-1">
            <div className="skeleton h-4 w-16" />
            <div className="skeleton h-10 w-full" />
          </div>
          <div className="grid gap-1 sm:col-span-3">
            <div className="skeleton h-4 w-40" />
            <div className="skeleton h-16 w-full" />
          </div>
        </div>
      </section>

      <section className="panel p-6">
        <div className="grid gap-2">
          <div className="skeleton h-6 w-44" />
          <div className="skeleton h-4 w-72" />
        </div>
        <div className="mt-4 grid gap-4">
          <div className="skeleton h-28 w-full" />
          <div className="skeleton h-28 w-full" />
          <div className="skeleton h-28 w-full" />
        </div>
      </section>

      <section className="panel p-6">
        <div className="grid gap-2">
          <div className="skeleton h-6 w-60" />
          <div className="skeleton h-4 w-full max-w-2xl" />
          <div className="skeleton h-4 w-full max-w-xl" />
        </div>
      </section>
    </div>
  );
}

function SettingsPageErrorState(props: { message: string; code: string; requestId?: string; onRetry: () => void }) {
  return (
    <div className="grid gap-6 pb-24">
      <div className="error-card">
        <div className="state-title">加载失败</div>
        <div className="state-desc">{formatApiErrorFields(props)}</div>
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

export function SettingsPage() {
  const state = useSettingsPageState();

  if (state.loading) return <SettingsPageSkeleton />;

  if (state.blockingLoadError) {
    return (
      <SettingsPageErrorState
        message={state.blockingLoadError.message}
        code={state.blockingLoadError.code}
        requestId={state.blockingLoadError.requestId}
        onRetry={() => void state.reloadAll()}
      />
    );
  }

  return (
    <div className="grid gap-6 pb-24">
      {state.dirty && state.outletActive ? <UnsavedChangesGuard when={state.dirty} /> : null}
      {state.refreshLoadError ? (
        <QueryErrorCard
          error={state.refreshLoadError}
          onRetry={() => void state.reloadAll()}
          retryBlockedReason={state.dirty ? "请先保存或放弃未保存修改再重试" : undefined}
          variant="warning"
        />
      ) : null}
      <SettingsCoreSections {...state.coreSectionsProps} />
      <div className="text-xs text-subtext">快捷键：Ctrl/Cmd + S 保存</div>
      <WizardNextBar {...state.wizardBarProps} />
    </div>
  );
}
