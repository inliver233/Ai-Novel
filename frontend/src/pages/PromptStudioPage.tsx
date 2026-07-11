import { CategoryListPanel } from "./promptStudio/CategoryListPanel";
import { PresetEditorPanel } from "./promptStudio/PresetEditorPanel";
import { usePromptStudio } from "./promptStudio/usePromptStudio";

export function PromptStudioPage() {
  const studio = usePromptStudio();

  if (!studio.projectId) {
    return <div className="text-subtext">缺少 projectId</div>;
  }

  if (studio.loading && !studio.categories.length) {
    return (
      <div className="grid gap-6" aria-busy="true" aria-live="polite">
        <div className="panel p-5">
          <div className="skeleton h-7 w-40" />
          <div className="mt-2 skeleton h-4 w-96 max-w-full" />
        </div>
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[240px_minmax(0,1fr)]">
          <div className="panel p-4">
            <div className="skeleton h-5 w-16" />
            <div className="mt-4 grid gap-2">
              <div className="skeleton h-16 w-full" />
              <div className="skeleton h-16 w-full" />
              <div className="skeleton h-16 w-full" />
            </div>
          </div>
          <div className="panel p-4">
            <div className="skeleton h-10 w-full" />
            <div className="mt-4 skeleton h-[480px] w-full" />
          </div>
        </div>
      </div>
    );
  }

  if (studio.loadError && !studio.categories.length) {
    return (
      <div className="grid gap-6">
        <div className="panel p-5">
          <div className="text-lg font-semibold text-ink">提示词工作室</div>
          <div className="mt-2 rounded-atelier border border-danger/30 bg-danger/5 px-4 py-3 text-sm text-subtext">
            {studio.loadError.message} ({studio.loadError.code})
            {studio.loadError.requestId ? ` | request_id: ${studio.loadError.requestId}` : ""}
          </div>
          <div className="mt-4">
            <button className="btn btn-primary" onClick={() => void studio.loadCategories()} type="button">
              重试
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="grid gap-6">
      <div className="panel p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-2xl font-semibold text-ink">提示词工作室</div>
            <div className="mt-1 text-sm text-subtext">
              按分类管理项目 Prompt Studio 预设。每个分类只会有一个生效预设，保存后即可在后端门面 API 中使用。
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button
              className="btn btn-primary"
              disabled={studio.loading || studio.busy}
              onClick={() => void studio.syncBuiltinDefaults()}
              type="button"
            >
              同步内置提示词
            </button>
            <button
              className="btn btn-secondary"
              disabled={studio.loading || studio.busy}
              onClick={() => void studio.loadCategories()}
              type="button"
            >
              刷新
            </button>
            <div className="text-xs text-subtext">{studio.busy || studio.presetLoading ? "处理中…" : ""}</div>
          </div>
        </div>

        {studio.loadError ? (
          <div className="mt-3 rounded-atelier border border-warning/30 bg-warning/5 px-3 py-2 text-sm text-subtext">
            最近一次刷新失败：{studio.loadError.message} ({studio.loadError.code})
            {studio.loadError.requestId ? ` | request_id: ${studio.loadError.requestId}` : ""}
          </div>
        ) : null}
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[240px_minmax(0,1fr)]">
        <CategoryListPanel
          categories={studio.categories}
          selectedKey={studio.selectedCategoryKey}
          onSelect={studio.selectCategory}
        />

        <PresetEditorPanel
          category={studio.selectedCategory}
          selectedPresetId={studio.selectedPresetId}
          onSelectPreset={studio.selectPreset}
          presetDetail={studio.presetDetail}
          draftName={studio.draftName}
          draftContent={studio.draftContent}
          onDraftNameChange={studio.setDraftName}
          onDraftContentChange={studio.setDraftContent}
          loading={studio.presetLoading}
          busy={studio.busy}
          hasChanges={studio.hasChanges}
          error={studio.presetError}
          onCreatePreset={studio.createPreset}
          onSave={studio.updatePreset}
          onActivate={studio.activatePreset}
          onDelete={studio.deletePreset}
        />
      </div>
    </div>
  );
}
