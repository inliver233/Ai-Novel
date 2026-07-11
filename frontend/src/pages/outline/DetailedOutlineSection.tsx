import { useState } from "react";
import clsx from "clsx";

import { MarkdownEditor } from "../../components/atelier/MarkdownEditor";
import { QueryErrorCard } from "../../components/atelier/QueryErrorCard";
import { Modal } from "../../components/ui/Modal";
import { ProgressBar } from "../../components/ui/ProgressBar";
import type {
  ChapterSkeletonGenerateRequest,
  DetailedOutlineGenerateRequest,
} from "../../services/detailedOutlinesApi";

import { OUTLINE_COPY } from "./outlineCopy";
import type { DetailedOutlineState } from "./useDetailedOutlineState";

/* ------------------------------------------------------------------ */
/*  Volume list sidebar                                                */
/* ------------------------------------------------------------------ */

type VolumeListProps = Pick<DetailedOutlineState, "items" | "selectedId" | "selectVolume" | "editing">;

function VolumeList(props: VolumeListProps) {
  const copy = OUTLINE_COPY.detailedOutline;

  if (props.items.length === 0) {
    return <div className="py-8 text-center text-xs text-subtext">{copy.noDetailedOutlines}</div>;
  }

  return (
    <ul className="grid gap-1">
      {props.items.map((item) => {
        const isActive = props.selectedId === item.id;
        const switchBlocked = props.editing && !isActive;
        return (
          <li key={item.id}>
            <button
              type="button"
              className={clsx(
                "w-full rounded-atelier px-3 py-2 text-left ui-transition-fast",
                isActive ? "bg-accent/10 text-accent" : "hover:bg-canvas text-ink",
              )}
              disabled={switchBlocked}
              onClick={() => void props.selectVolume(item.id)}
              title={switchBlocked ? "请先保存或取消当前编辑，再切换细纲" : undefined}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium truncate">
                  {copy.volumePrefix}
                  {item.volume_number}
                  {copy.volumeSuffix} {item.volume_title}
                </span>
                <StatusBadge status={item.status} />
              </div>
              <div className="mt-0.5 text-[11px] text-subtext">
                {item.chapter_count}
                {copy.chapterCountSuffix}
              </div>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/* ------------------------------------------------------------------ */
/*  Status badge                                                       */
/* ------------------------------------------------------------------ */

function StatusBadge(props: { status: "planned" | "generating" | "done" }) {
  const copy = OUTLINE_COPY.detailedOutline;
  const labels: Record<string, string> = {
    planned: copy.statusPlanned,
    generating: copy.statusGenerating,
    done: copy.statusDone,
  };
  const colors: Record<string, string> = {
    planned: "bg-border text-subtext",
    generating: "bg-yellow-400/20 text-yellow-600 animate-pulse",
    done: "bg-green-400/20 text-green-600",
  };

  return (
    <span
      className={clsx(
        "shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium",
        colors[props.status] ?? "bg-border text-subtext",
      )}
    >
      {labels[props.status] ?? props.status}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/*  Volume detail panel                                                */
/* ------------------------------------------------------------------ */

type VolumeDetailProps = Pick<
  DetailedOutlineState,
  | "selected"
  | "editing"
  | "editContent"
  | "editTitle"
  | "saving"
  | "startEdit"
  | "cancelEdit"
  | "setEditContent"
  | "setEditTitle"
  | "saveEdit"
  | "deleteVolume"
  | "createChapters"
  | "skeletonGenerating"
  | "openSkeletonModal"
>;

function VolumeDetail(props: VolumeDetailProps) {
  const copy = OUTLINE_COPY.detailedOutline;

  if (!props.selected) {
    return (
      <div className="flex h-full items-center justify-center py-16 text-sm text-subtext">{copy.selectVolumeHint}</div>
    );
  }

  const vol = props.selected;

  return (
    <div className="grid gap-4">
      {/* Title area */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          {props.editing ? (
            <input
              className="input text-sm"
              value={props.editTitle}
              onChange={(e) => props.setEditTitle(e.target.value)}
            />
          ) : (
            <div className="text-sm font-medium text-ink">
              {copy.volumePrefix}
              {vol.volume_number}
              {copy.volumeSuffix} {vol.volume_title}
            </div>
          )}
          <StatusBadge status={vol.status} />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {props.editing ? (
            <>
              <button className="btn btn-secondary" type="button" onClick={props.cancelEdit}>
                {OUTLINE_COPY.cancel}
              </button>
              <button
                className="btn btn-primary"
                type="button"
                disabled={props.saving}
                onClick={() => void props.saveEdit()}
              >
                {props.saving ? copy.savingButton : OUTLINE_COPY.save}
              </button>
            </>
          ) : (
            <div className="grid grid-cols-2 gap-2 sm:flex sm:items-center sm:gap-2">
              <button className="btn btn-secondary" type="button" onClick={props.startEdit}>
                {copy.editButton}
              </button>
              <button className="btn btn-secondary" type="button" onClick={() => void props.createChapters(vol.id)}>
                {copy.createChaptersFromDetailed}
              </button>
              <button
                className="btn btn-primary"
                type="button"
                disabled={props.skeletonGenerating}
                onClick={() => props.openSkeletonModal()}
              >
                {props.skeletonGenerating
                  ? OUTLINE_COPY.detailedOutline.generatingSkeletonButton
                  : OUTLINE_COPY.detailedOutline.generateSkeletonButton}
              </button>
              <button
                className="btn btn-ghost col-span-2 text-danger hover:bg-danger/10 sm:col-span-1"
                type="button"
                onClick={() => void props.deleteVolume(vol.id)}
              >
                {OUTLINE_COPY.delete}
              </button>
            </div>
          )}
        </div>
      </div>

      {/* Content area */}
      {props.editing ? (
        <MarkdownEditor
          value={props.editContent}
          onChange={props.setEditContent}
          placeholder={copy.editorPlaceholder}
          minRows={12}
          name="detailed_outline_content"
        />
      ) : (
        <MarkdownEditor
          value={vol.content_md ?? ""}
          onChange={() => {}}
          minRows={12}
          name="detailed_outline_content_readonly"
          readOnly
        />
      )}

      {/* Structure summary */}
      {vol.structure ? (
        <details className="panel p-3">
          <summary className="cursor-pointer text-xs text-subtext ui-transition-fast hover:text-ink">
            {copy.structureSummaryTitle}
          </summary>
          <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words text-xs text-ink">
            {typeof vol.structure === "string" ? vol.structure : JSON.stringify(vol.structure, null, 2)}
          </pre>
        </details>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Main section (two-panel layout)                                    */
/* ------------------------------------------------------------------ */

export type DetailedOutlineSectionProps = DetailedOutlineState;

export function DetailedOutlineSection(props: DetailedOutlineSectionProps) {
  const copy = OUTLINE_COPY.detailedOutline;
  const blockingListError = !props.hasData ? props.error : null;
  const refreshListError = props.hasData ? props.error : null;
  const blockingDetailError = !props.detailHasData ? props.detailError : null;
  const refreshDetailError = props.detailHasData ? props.detailError : null;
  const detailRetryBlockedReason = props.editing ? "请先保存或取消当前编辑，再重试加载详情" : undefined;

  return (
    <>
      {/* Guide panel */}
      <div className="panel p-6 sm:p-8">
        <div className="text-sm text-ink">{copy.detailedFlowTitle}</div>
        <div className="mt-1 text-xs text-subtext">{copy.detailedFlowDescription}</div>
        <div className="mt-1 text-[11px] text-subtext">{copy.detailedFlowHint}</div>
      </div>

      {blockingListError ? (
        <QueryErrorCard error={blockingListError} onRetry={() => void props.reload()} title="细纲列表加载失败" />
      ) : null}

      {!props.hasData && props.loading ? (
        <div className="panel p-8 text-center text-sm text-subtext">加载中...</div>
      ) : null}

      {refreshListError ? (
        <QueryErrorCard
          error={refreshListError}
          onRetry={() => void props.reload()}
          title="细纲列表刷新失败"
          variant="warning"
        />
      ) : null}

      {props.hasData ? (
        <div className="flex flex-wrap items-center gap-2">
          <button
            className={props.items.length === 0 ? "btn btn-primary" : "btn btn-secondary"}
            type="button"
            onClick={props.openGenerateModal}
            disabled={props.generating}
          >
            {props.generating ? copy.generatingDetailedButton : copy.generateDetailedButton}
          </button>
        </div>
      ) : null}

      {/* Two-panel content */}
      {props.hasData && props.items.length > 0 ? (
        <div className="grid gap-4 sm:grid-cols-[240px_1fr]">
          {/* Left panel: volume list */}
          <div className="panel p-3 sm:p-4 overflow-y-auto sm:max-h-[70vh]">
            <VolumeList
              editing={props.editing}
              items={props.items}
              selectedId={props.selectedId}
              selectVolume={props.selectVolume}
            />
          </div>

          {/* Right panel: detail */}
          <div className="panel p-4 sm:p-6">
            {blockingDetailError ? (
              <QueryErrorCard
                error={blockingDetailError}
                onRetry={() => void props.reloadDetail()}
                retryBlockedReason={detailRetryBlockedReason}
                title="细纲详情加载失败"
              />
            ) : props.detailLoading && !props.detailHasData ? (
              <div className="py-16 text-center text-sm text-subtext">加载详情中...</div>
            ) : (
              <div className="grid gap-4">
                {refreshDetailError ? (
                  <QueryErrorCard
                    error={refreshDetailError}
                    onRetry={() => void props.reloadDetail()}
                    retryBlockedReason={detailRetryBlockedReason}
                    title="细纲详情刷新失败"
                    variant="warning"
                  />
                ) : null}
                <VolumeDetail
                  selected={props.selected}
                  editing={props.editing}
                  editContent={props.editContent}
                  editTitle={props.editTitle}
                  saving={props.saving}
                  startEdit={props.startEdit}
                  cancelEdit={props.cancelEdit}
                  setEditContent={props.setEditContent}
                  setEditTitle={props.setEditTitle}
                  saveEdit={props.saveEdit}
                  deleteVolume={props.deleteVolume}
                  createChapters={props.createChapters}
                  skeletonGenerating={props.skeletonGenerating}
                  openSkeletonModal={props.openSkeletonModal}
                />
              </div>
            )}
          </div>
        </div>
      ) : props.hasData && !props.generating ? (
        <div className="panel p-8 text-center text-sm text-subtext">{copy.noDetailedOutlines}</div>
      ) : null}

      {/* Progress during generation */}
      {props.generating && props.progress ? (
        <div className="panel p-4">
          <div className="flex items-center justify-between gap-2 text-xs text-subtext">
            <span className="truncate">{props.progress.message}</span>
            <span className="shrink-0">
              {props.progress.total > 0 ? `${props.progress.current}/${props.progress.total}` : "..."}
            </span>
          </div>
          <ProgressBar
            ariaLabel={copy.progressLabel}
            value={props.progress.total > 0 ? (props.progress.current / props.progress.total) * 100 : 0}
          />
          <div className="mt-2 flex justify-end">
            <button className="btn btn-secondary" type="button" onClick={props.cancelGenerate}>
              {OUTLINE_COPY.detailedOutline.cancelGenerateButton}
            </button>
          </div>
        </div>
      ) : null}

      {props.skeletonGenerating && props.skeletonProgress ? (
        <div className="panel p-4">
          <div className="flex items-center justify-between gap-2 text-xs text-subtext">
            <span className="truncate">{props.skeletonProgress.message}</span>
            <span className="shrink-0">{props.skeletonProgress.current}%</span>
          </div>
          <ProgressBar
            ariaLabel={OUTLINE_COPY.detailedOutline.skeletonProgressLabel}
            value={props.skeletonProgress.current}
          />
          <div className="mt-2 flex justify-end">
            <button className="btn btn-secondary" type="button" onClick={props.cancelSkeletonGenerate}>
              {OUTLINE_COPY.cancel}
            </button>
          </div>
        </div>
      ) : null}

      <ChapterSkeletonGenerationModal
        open={props.skeletonModalOpen}
        generating={props.skeletonGenerating}
        progress={props.skeletonProgress}
        detailedOutlineId={props.selected?.id}
        streamRawText={props.skeletonStreamRawText}
        streamResult={props.skeletonStreamResult}
        onClose={props.closeSkeletonModal}
        onGenerate={props.generateChapterSkeleton}
        onCancelGenerate={props.cancelSkeletonGenerate}
      />
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  Generation modal                                                   */
/* ------------------------------------------------------------------ */

export type DetailedOutlineGenerationModalProps = {
  open: boolean;
  generating: boolean;
  progress: DetailedOutlineState["progress"];
  onClose: () => void;
  onGenerate: (request: DetailedOutlineGenerateRequest) => void;
  onCancelGenerate: () => void;
};

export function DetailedOutlineGenerationModal(props: DetailedOutlineGenerationModalProps) {
  const copy = OUTLINE_COPY.detailedOutline;

  const [chaptersPerVolume, setChaptersPerVolume] = useState<string>("");
  const [instruction, setInstruction] = useState("");
  const [includeWorldSetting, setIncludeWorldSetting] = useState(true);
  const [includeCharacters, setIncludeCharacters] = useState(true);

  const handleGenerate = () => {
    const parsed = chaptersPerVolume.trim() ? Number(chaptersPerVolume) : null;
    props.onGenerate({
      chapters_per_volume: parsed && Number.isFinite(parsed) && parsed > 0 ? parsed : null,
      instruction: instruction.trim() || null,
      context: {
        include_world_setting: includeWorldSetting,
        include_characters: includeCharacters,
      },
    });
  };

  return (
    <Modal
      open={props.open}
      onClose={props.onClose}
      panelClassName="surface max-w-2xl p-4 sm:p-6"
      ariaLabel={copy.generateDetailedTitle}
    >
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="font-content text-xl sm:text-2xl">{copy.generateDetailedTitle}</div>
          <div className="mt-1 text-xs text-subtext">{copy.generateDetailedHint}</div>
        </div>
        <button className="btn btn-secondary" onClick={props.onClose} type="button">
          {OUTLINE_COPY.close}
        </button>
      </div>

      <div className="mt-4 grid gap-4">
        {/* Basic params */}
        <div className="rounded-atelier border border-border bg-canvas p-4">
          <div className="text-sm text-ink">{copy.generateFormTitle}</div>
          <div className="mt-1 text-xs text-subtext">{copy.generateFormHint}</div>
          <div className="mt-3 grid gap-4 sm:grid-cols-2">
            <label className="grid gap-1">
              <span className="text-xs text-subtext">{copy.chaptersPerVolumeLabel}</span>
              <input
                className="input"
                type="number"
                min={1}
                name="chapters_per_volume"
                value={chaptersPerVolume}
                onChange={(e) => setChaptersPerVolume(e.target.value)}
                placeholder={copy.chaptersPerVolumePlaceholder}
              />
            </label>
            <label className="grid gap-1 sm:col-span-2">
              <span className="text-xs text-subtext">{copy.instructionLabel}</span>
              <textarea
                className="input resize-y"
                name="instruction"
                rows={3}
                value={instruction}
                onChange={(e) => setInstruction(e.target.value)}
                placeholder={copy.instructionPlaceholder}
              />
            </label>
          </div>
        </div>

        {/* Advanced params */}
        <div className="rounded-atelier border border-border bg-canvas p-4">
          <div className="text-sm text-ink">{copy.advancedTitle}</div>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <label className="flex items-center gap-2 text-sm text-ink">
              <input
                className="checkbox"
                checked={includeWorldSetting}
                name="include_world_setting"
                onChange={(e) => setIncludeWorldSetting(e.target.checked)}
                type="checkbox"
              />
              {OUTLINE_COPY.includeWorldSetting}
            </label>
            <label className="flex items-center gap-2 text-sm text-ink">
              <input
                className="checkbox"
                checked={includeCharacters}
                name="include_characters"
                onChange={(e) => setIncludeCharacters(e.target.checked)}
                type="checkbox"
              />
              {OUTLINE_COPY.includeCharacters}
            </label>
          </div>
        </div>
      </div>

      {/* Progress section */}
      {props.generating && props.progress ? (
        <div className="mt-4 panel p-3">
          <div className="flex items-center justify-between gap-2 text-xs text-subtext">
            <span className="truncate">{props.progress.message}</span>
            <span className="shrink-0">
              {props.progress.total > 0 ? `${props.progress.current}/${props.progress.total}` : "..."}
            </span>
          </div>
          <ProgressBar
            ariaLabel={copy.progressLabel}
            value={props.progress.total > 0 ? (props.progress.current / props.progress.total) * 100 : 0}
          />
        </div>
      ) : null}

      <div className="mt-5 flex flex-wrap justify-end gap-2">
        <button className="btn btn-secondary" onClick={props.onClose} type="button">
          {OUTLINE_COPY.cancel}
        </button>
        {props.generating ? (
          <button className="btn btn-secondary" onClick={props.onCancelGenerate} type="button">
            {copy.cancelGenerateButton}
          </button>
        ) : null}
        <button className="btn btn-primary" disabled={props.generating} onClick={handleGenerate} type="button">
          {props.generating ? copy.generatingDetailedButton : copy.generateDetailedButton}
        </button>
      </div>
    </Modal>
  );
}

export type ChapterSkeletonGenerationModalProps = {
  open: boolean;
  generating: boolean;
  progress: DetailedOutlineState["skeletonProgress"];
  detailedOutlineId: string | undefined;
  streamRawText: string;
  streamResult: Record<string, unknown> | null;
  onClose: () => void;
  onGenerate: (detailedOutlineId: string, request: ChapterSkeletonGenerateRequest) => void;
  onCancelGenerate: () => void;
};

export function ChapterSkeletonGenerationModal(props: ChapterSkeletonGenerationModalProps) {
  const copy = OUTLINE_COPY.detailedOutline;

  const [chaptersCount, setChaptersCount] = useState<string>("");
  const [instruction, setInstruction] = useState("");
  const [includeWorldSetting, setIncludeWorldSetting] = useState(true);
  const [includeCharacters, setIncludeCharacters] = useState(true);
  const [replaceChapters, setReplaceChapters] = useState(true);

  const handleGenerate = () => {
    if (!props.detailedOutlineId) return;
    const parsed = chaptersCount.trim() ? Number(chaptersCount) : null;
    props.onGenerate(props.detailedOutlineId, {
      chapters_count: parsed && Number.isFinite(parsed) && parsed > 0 ? parsed : null,
      instruction: instruction.trim() || null,
      context: {
        include_world_setting: includeWorldSetting,
        include_characters: includeCharacters,
      },
      replace_chapters: replaceChapters,
    });
  };

  return (
    <Modal
      open={props.open}
      onClose={props.onClose}
      panelClassName="surface max-w-2xl p-4 sm:p-6"
      ariaLabel={copy.generateSkeletonTitle}
    >
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="font-content text-xl sm:text-2xl">{copy.generateSkeletonTitle}</div>
          <div className="mt-1 text-xs text-subtext">{copy.generateSkeletonHint}</div>
        </div>
        <button className="btn btn-secondary" onClick={props.onClose} type="button">
          {OUTLINE_COPY.close}
        </button>
      </div>

      <div className="mt-4 grid gap-4">
        <div className="rounded-atelier border border-border bg-canvas p-4">
          <div className="mt-3 grid gap-4 sm:grid-cols-2">
            <label className="grid gap-1">
              <span className="text-xs text-subtext">{copy.skeletonChaptersCountLabel}</span>
              <input
                className="input"
                type="number"
                min={3}
                max={50}
                name="chapters_count"
                value={chaptersCount}
                onChange={(e) => setChaptersCount(e.target.value)}
                placeholder={copy.skeletonChaptersCountPlaceholder}
              />
            </label>
            <label className="grid gap-1 sm:col-span-2">
              <span className="text-xs text-subtext">{copy.skeletonInstructionLabel}</span>
              <textarea
                className="input resize-y"
                name="instruction"
                rows={3}
                value={instruction}
                onChange={(e) => setInstruction(e.target.value)}
                placeholder={copy.skeletonInstructionPlaceholder}
              />
            </label>
          </div>
        </div>

        <div className="rounded-atelier border border-border bg-canvas p-4">
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <label className="flex items-center gap-2 text-sm text-ink">
              <input
                className="checkbox"
                checked={includeWorldSetting}
                name="include_world_setting"
                onChange={(e) => setIncludeWorldSetting(e.target.checked)}
                type="checkbox"
              />
              {OUTLINE_COPY.includeWorldSetting}
            </label>
            <label className="flex items-center gap-2 text-sm text-ink">
              <input
                className="checkbox"
                checked={includeCharacters}
                name="include_characters"
                onChange={(e) => setIncludeCharacters(e.target.checked)}
                type="checkbox"
              />
              {OUTLINE_COPY.includeCharacters}
            </label>
            <label className="flex items-center gap-2 text-sm text-ink">
              <input
                className="checkbox"
                checked={replaceChapters}
                name="replace_chapters"
                onChange={(e) => setReplaceChapters(e.target.checked)}
                type="checkbox"
              />
              {copy.skeletonReplaceLabel}
            </label>
          </div>
          <div className="mt-1 text-[11px] text-subtext">{copy.skeletonReplaceHint}</div>
        </div>
      </div>

      {props.generating && props.progress ? (
        <div className="mt-4 panel p-3">
          <div className="flex items-center justify-between gap-2 text-xs text-subtext">
            <span className="truncate">{props.progress.message}</span>
            <span className="shrink-0">{props.progress.current}%</span>
          </div>
          <ProgressBar ariaLabel={copy.skeletonProgressLabel} value={props.progress.current} />
        </div>
      ) : null}

      {props.generating || props.streamRawText || props.streamResult ? (
        <div className="mt-4 grid gap-3">
          {props.streamRawText ? (
            <details open={props.generating} className="panel p-3">
              <summary className="cursor-pointer text-xs text-subtext ui-transition-fast hover:text-ink">
                {OUTLINE_COPY.detailedOutline.skeletonStreamRawTitle ?? "流式输出"}
              </summary>
              <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words text-xs text-ink sm:max-h-60">
                {props.streamRawText}
              </pre>
            </details>
          ) : null}

          {props.streamResult ? (
            <details open className="panel p-3">
              <summary className="cursor-pointer text-xs text-subtext ui-transition-fast hover:text-ink">
                {OUTLINE_COPY.detailedOutline.skeletonJsonPreviewTitle ?? "章节结构预览"}
              </summary>
              <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words text-xs text-ink sm:max-h-60">
                {JSON.stringify(props.streamResult, null, 2)}
              </pre>
            </details>
          ) : null}
        </div>
      ) : null}

      <div className="mt-5 flex flex-wrap justify-end gap-2">
        <button className="btn btn-secondary" onClick={props.onClose} type="button">
          {OUTLINE_COPY.cancel}
        </button>
        {props.generating ? (
          <button className="btn btn-secondary" onClick={props.onCancelGenerate} type="button">
            {OUTLINE_COPY.cancel}
          </button>
        ) : null}
        <button
          className="btn btn-primary"
          disabled={props.generating || !props.detailedOutlineId}
          onClick={handleGenerate}
          type="button"
        >
          {props.generating ? copy.generatingSkeletonButton : copy.generateSkeletonButton}
        </button>
      </div>
    </Modal>
  );
}
