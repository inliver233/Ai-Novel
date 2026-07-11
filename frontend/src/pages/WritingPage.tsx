import { WizardNextBar } from "../components/atelier/WizardNextBar";
import { QueryErrorCard } from "../components/atelier/QueryErrorCard";
import { ToolContent } from "../components/layout/AppShell";
import { UnsavedChangesGuard } from "../hooks/useUnsavedChangesGuard";

import {
  WritingChapterListDrawer,
  WritingPageOverlays,
  WritingStreamFloatingCard,
  WritingWorkspace,
} from "./writing/WritingPageSections";
import { WRITING_PAGE_COPY } from "./writing/writingPageCopy";
import { useWritingPageState } from "./writing/useWritingPageState";

export function WritingPage() {
  const state = useWritingPageState();

  if (state.loading) {
    return <ToolContent className="text-subtext">{WRITING_PAGE_COPY.loading}</ToolContent>;
  }

  if (state.metadataBlockingLoadError) {
    return (
      <ToolContent>
        <QueryErrorCard error={state.metadataBlockingLoadError} onRetry={() => void state.reloadMetadata()} />
      </ToolContent>
    );
  }

  if (state.chapterListBlockingLoadError) {
    return (
      <ToolContent>
        <QueryErrorCard
          error={state.chapterListBlockingLoadError}
          onRetry={() => void state.reloadChapterList()}
          title="章节列表加载失败"
        />
      </ToolContent>
    );
  }

  return (
    <ToolContent className="grid gap-4 pb-24">
      {state.showUnsavedGuard ? <UnsavedChangesGuard when={state.dirty} /> : null}
      {state.metadataRefreshLoadError ? (
        <QueryErrorCard
          error={state.metadataRefreshLoadError}
          onRetry={() => void state.reloadMetadata()}
          retryBlockedReason={state.dirty ? "请先保存或放弃未保存修改再重试" : undefined}
          variant="warning"
        />
      ) : null}
      {state.chapterListRefreshLoadError ? (
        <QueryErrorCard
          error={state.chapterListRefreshLoadError}
          onRetry={() => void state.reloadChapterList()}
          retryBlockedReason={state.dirty ? "请先保存或放弃未保存修改再重试" : undefined}
          title="最近一次章节列表刷新失败"
          variant="warning"
        />
      ) : null}
      <WritingWorkspace {...state.workspaceProps} />
      <WritingChapterListDrawer {...state.chapterListDrawerProps} />
      <WritingPageOverlays {...state.overlaysProps} />
      <WritingStreamFloatingCard {...state.streamFloatingProps} />
      <WizardNextBar {...state.wizardBarProps} />
    </ToolContent>
  );
}
