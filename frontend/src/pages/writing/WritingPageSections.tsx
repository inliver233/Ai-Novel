import type { ComponentProps } from "react";

import { GhostwriterIndicator } from "../../components/atelier/GhostwriterIndicator";
import { MarkdownEditor } from "../../components/atelier/MarkdownEditor";
import { QueryErrorCard } from "../../components/atelier/QueryErrorCard";
import { Drawer } from "../../components/ui/Drawer";
import { ProgressBar } from "../../components/ui/ProgressBar";
import { useIsMobile } from "../../hooks/useIsMobile";
import { AiGenerateDrawer } from "../../components/writing/AiGenerateDrawer";
import { BatchGenerationModal } from "../../components/writing/BatchGenerationModal";
import { ChapterListPanel } from "../../components/writing/ChapterListPanel";
import { CreateChapterDialog } from "../../components/writing/CreateChapterDialog";
import { GenerationHistoryDrawer } from "../../components/writing/GenerationHistoryDrawer";
import { PromptInspectorDrawer } from "../../components/writing/PromptInspectorDrawer";
import { WritingToolbar } from "../../components/writing/WritingToolbar";
import { humanizeChapterStatus } from "../../lib/humanize";
import type { ApiError } from "../../services/apiClient";
import type { Chapter, ChapterListItem, ChapterStatus } from "../../types";

import type { ChapterForm } from "./writingUtils";
import { CHAPTER_LIST_SIDEBAR_WIDTH_CLASS } from "./writingPageModels";
import {
  getWritingChapterHeading,
  getWritingGenerateIndicatorLabel,
  getWritingReadonlyCallout,
  getWritingStatusHint,
  WRITING_PAGE_COPY,
} from "./writingPageCopy";

export type WritingEditorSectionProps = {
  activeChapter: Chapter | null;
  form: ChapterForm | null;
  dirty: boolean;
  isDoneReadonly: boolean;
  loadingChapter: boolean;
  loadError: ApiError | null;
  generating: boolean;
  saving: boolean;
  autoUpdatesTriggering: boolean;
  contentEditorTab: "edit" | "preview";
  onContentEditorTabChange: (tab: "edit" | "preview") => void;
  onTitleChange: (value: string) => void;
  onStatusChange: (status: ChapterStatus) => void;
  onPlanChange: (value: string) => void;
  onContentChange: (value: string) => void;
  onSummaryChange: (value: string) => void;
  onDeleteChapter: () => void;
  onRetryLoad: () => void;
  onSaveAndTriggerAutoUpdates: () => void;
  onSaveChapter: () => void;
  onReopenDrafting: () => void;
  generationIndicatorLabel?: string;
};

export function WritingEditorSection(props: WritingEditorSectionProps) {
  if (props.loadingChapter && !props.activeChapter && !props.form) {
    return (
      <div className="mx-auto w-full max-w-4xl rounded-atelier border border-border bg-surface p-8 text-sm text-subtext shadow-sm">
        章节加载中...
      </div>
    );
  }

  if (props.loadError) {
    return (
      <div className="mx-auto w-full max-w-4xl">
        <QueryErrorCard error={props.loadError} onRetry={props.onRetryLoad} title="章节加载失败" />
      </div>
    );
  }

  if (!props.activeChapter || !props.form) {
    return (
      <div className="mx-auto w-full max-w-4xl rounded-atelier border border-border bg-surface p-8 text-sm text-subtext shadow-sm">
        {WRITING_PAGE_COPY.emptyState}
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-4xl rounded-atelier border border-border bg-surface p-3 sm:p-5 shadow-sm">
      {props.isDoneReadonly ? (
        <div className="callout-warning mb-4 flex flex-wrap items-center justify-between gap-2">
          <div className="text-xs">{getWritingReadonlyCallout()}</div>
          <button className="btn btn-secondary" onClick={props.onReopenDrafting} type="button">
            {WRITING_PAGE_COPY.readonlyCalloutAction}
          </button>
        </div>
      ) : null}

      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="font-content text-lg sm:text-2xl text-ink">
            {getWritingChapterHeading(props.activeChapter.number)}{" "}
            <span className="text-subtext">{props.dirty ? WRITING_PAGE_COPY.dirtyBadge : ""}</span>
          </div>
          <div className="mt-1 text-xs text-subtext">
            {WRITING_PAGE_COPY.updatedAtPrefix} {props.activeChapter.updated_at}
          </div>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-1.5 sm:gap-2">
          <button
            className="btn btn-ghost text-accent hover:bg-accent/10"
            disabled={props.loadingChapter || props.generating}
            onClick={props.onDeleteChapter}
            type="button"
          >
            {WRITING_PAGE_COPY.delete}
          </button>
          <button
            className="btn btn-secondary"
            disabled={
              props.loadingChapter || props.generating || props.saving || props.autoUpdatesTriggering || !props.dirty
            }
            onClick={props.onSaveAndTriggerAutoUpdates}
            type="button"
          >
            {props.autoUpdatesTriggering ? WRITING_PAGE_COPY.saveAndTriggerPending : WRITING_PAGE_COPY.saveAndTrigger}
          </button>
          <button
            className="btn btn-primary"
            disabled={!props.dirty || props.saving || props.loadingChapter || props.generating}
            onClick={props.onSaveChapter}
            type="button"
          >
            {props.saving ? WRITING_PAGE_COPY.saving : WRITING_PAGE_COPY.save}
          </button>
        </div>
      </div>

      {props.generating ? (
        <GhostwriterIndicator
          className="mt-4"
          label={props.generationIndicatorLabel ?? getWritingGenerateIndicatorLabel()}
        />
      ) : null}

      <div className="mt-4 grid gap-3 sm:grid-cols-3">
        <label className="grid gap-1 sm:col-span-2">
          <span className="text-xs text-subtext">{WRITING_PAGE_COPY.titleLabel}</span>
          <input
            className="input-underline font-content text-xl"
            name="title"
            value={props.form.title}
            readOnly={props.isDoneReadonly}
            onChange={(event) => props.onTitleChange(event.target.value)}
          />
        </label>
        <label className="grid gap-1 sm:col-span-1">
          <span className="text-xs text-subtext">{WRITING_PAGE_COPY.statusLabel}</span>
          <select
            className="select"
            name="status"
            value={props.form.status}
            onChange={(event) => props.onStatusChange(event.target.value as ChapterStatus)}
          >
            <option value="planned">{humanizeChapterStatus("planned")}</option>
            <option value="drafting">{humanizeChapterStatus("drafting")}</option>
            <option value="done">{humanizeChapterStatus("done")}</option>
          </select>
          <div className="text-[11px] text-subtext">{getWritingStatusHint()}</div>
        </label>
      </div>

      <div className="mt-4 grid gap-3">
        <label className="grid gap-1">
          <span className="text-xs text-subtext">{WRITING_PAGE_COPY.planLabel}</span>
          <textarea
            className="textarea atelier-content"
            name="plan"
            rows={4}
            value={props.form.plan}
            readOnly={props.isDoneReadonly}
            onChange={(event) => props.onPlanChange(event.target.value)}
          />
        </label>
        <label className="grid gap-1">
          <span className="text-xs text-subtext">{WRITING_PAGE_COPY.contentLabel}</span>
          <MarkdownEditor
            value={props.form.content_md}
            onChange={props.onContentChange}
            placeholder={WRITING_PAGE_COPY.contentPlaceholder}
            minRows={16}
            name="content_md"
            readOnly={props.isDoneReadonly}
            tab={props.contentEditorTab}
            onTabChange={props.onContentEditorTabChange}
          />
        </label>
        <label className="grid gap-1">
          <span className="text-xs text-subtext">{WRITING_PAGE_COPY.summaryLabel}</span>
          <textarea
            className="textarea atelier-content"
            name="summary"
            rows={3}
            value={props.form.summary}
            readOnly={props.isDoneReadonly}
            onChange={(event) => props.onSummaryChange(event.target.value)}
          />
        </label>
      </div>

      <div className="mt-4 text-xs text-subtext">{WRITING_PAGE_COPY.hotkeyHint}</div>
    </div>
  );
}

export type WritingWorkspaceProps = {
  toolbarProps: Omit<ComponentProps<typeof WritingToolbar>, "onOpenChapterList">;
  chapterListProps: {
    chapters: ChapterListItem[];
    activeId: string | null;
    onSelectChapter: (chapterId: string) => void;
    onOpenDrawer: () => void;
  };
  editorProps: WritingEditorSectionProps;
};

export function WritingWorkspace(props: WritingWorkspaceProps) {
  return (
    <>
      <WritingToolbar {...props.toolbarProps} onOpenChapterList={props.chapterListProps.onOpenDrawer} />
      <div className="flex gap-4">
        <aside className={`hidden ${CHAPTER_LIST_SIDEBAR_WIDTH_CLASS} shrink-0 lg:block`}>
          <ChapterListPanel
            chapters={props.chapterListProps.chapters}
            activeId={props.chapterListProps.activeId}
            onSelectChapter={props.chapterListProps.onSelectChapter}
          />
        </aside>

        <section className="min-w-0 flex-1">
          <WritingEditorSection {...props.editorProps} />
        </section>
      </div>
    </>
  );
}

export type WritingChapterListDrawerProps = {
  open: boolean;
  chapters: ChapterListItem[];
  activeId: string | null;
  onClose: () => void;
  onSelectChapter: (chapterId: string) => void;
};

export function WritingChapterListDrawer(props: WritingChapterListDrawerProps) {
  const isMobile = useIsMobile();

  return (
    <Drawer
      open={props.open}
      onClose={props.onClose}
      side={isMobile ? "bottom" : "left"}
      overlayClassName="lg:hidden"
      ariaLabel="章节列表"
      panelClassName={`h-[75dvh] sm:h-full w-full sm:${CHAPTER_LIST_SIDEBAR_WIDTH_CLASS} overflow-hidden border-t sm:border-t-0 sm:border-r border-border bg-surface shadow-sm rounded-t-atelier sm:rounded-none`}
    >
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="text-sm text-ink">章节列表</div>
        <button className="btn btn-secondary" onClick={props.onClose} type="button">
          关闭
        </button>
      </div>

      <div className="h-full p-2">
        <ChapterListPanel
          chapters={props.chapters}
          activeId={props.activeId}
          containerClassName="h-full"
          onSelectChapter={(chapterId) => {
            props.onClose();
            props.onSelectChapter(chapterId);
          }}
        />
      </div>
    </Drawer>
  );
}

export type WritingStreamFloatingCardProps = {
  open: boolean;
  requestId: string | null;
  message?: string;
  progress: number;
  onExpand: () => void;
  onCancel: () => void;
};

export function WritingStreamFloatingCard(props: WritingStreamFloatingCardProps) {
  if (!props.open) return null;

  return (
    <div className="fixed inset-x-4 bottom-24 z-40 flex justify-center sm:inset-auto sm:bottom-8 sm:right-8 sm:justify-end">
      <div className="w-full max-w-sm rounded-atelier border border-border bg-surface/90 p-3 shadow-sm backdrop-blur">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="text-sm text-ink">{WRITING_PAGE_COPY.streamFloatingTitle}</div>
            <div className="mt-1 truncate text-xs text-subtext">
              {props.message ?? WRITING_PAGE_COPY.streamFloatingPending}
            </div>
            {props.requestId ? (
              <div className="mt-1 truncate text-[11px] text-subtext">request_id: {props.requestId}</div>
            ) : null}
          </div>
          <div className="shrink-0 text-xs text-subtext">{Math.max(0, Math.min(100, props.progress))}%</div>
        </div>
        <ProgressBar ariaLabel="写作页流式生成进度" className="mt-2" value={props.progress} />
        <div className="mt-3 flex justify-end gap-2">
          <button className="btn btn-secondary" onClick={props.onExpand} type="button">
            {WRITING_PAGE_COPY.streamFloatingExpand}
          </button>
          <button className="btn btn-secondary" onClick={props.onCancel} type="button">
            {WRITING_PAGE_COPY.cancel}
          </button>
        </div>
      </div>
    </div>
  );
}

export type WritingPageOverlaysProps = {
  createChapterDialogProps: ComponentProps<typeof CreateChapterDialog>;
  batchGenerationModalProps: ComponentProps<typeof BatchGenerationModal>;
  aiGenerateDrawerProps: ComponentProps<typeof AiGenerateDrawer>;
  promptInspectorDrawerProps: ComponentProps<typeof PromptInspectorDrawer>;
  generationHistoryDrawerProps: ComponentProps<typeof GenerationHistoryDrawer>;
};

export function WritingPageOverlays(props: WritingPageOverlaysProps) {
  return (
    <>
      <CreateChapterDialog {...props.createChapterDialogProps} />
      <BatchGenerationModal {...props.batchGenerationModalProps} />
      <AiGenerateDrawer {...props.aiGenerateDrawerProps} />
      <PromptInspectorDrawer {...props.promptInspectorDrawerProps} />
      <GenerationHistoryDrawer {...props.generationHistoryDrawerProps} />
    </>
  );
}
