import { useRef } from "react";
import { MarkdownEditor } from "../../components/atelier/MarkdownEditor";
import { Modal } from "../../components/ui/Modal";
import { ProgressBar } from "../../components/ui/ProgressBar";
import type { OutlineListItem } from "../../types";
import type { OutlineGenResult } from "../outlineParsing";

import { OUTLINE_COPY, getOutlinePreviewMetaText, getOutlineTitleModalLabel } from "./outlineCopy";
import type { OutlineGenForm, OutlineStreamProgress } from "./outlineModels";

export type OutlineHeaderSectionProps = {
  outlines: OutlineListItem[];
  activeOutlineId: string;
  activeOutlineHasChapters: boolean;
  onSwitchOutline: (outlineId: string) => void;
  onOpenCreate: () => void;
  onOpenRename: () => void;
  onDelete: () => void;
};

export function OutlineHeaderSection(props: OutlineHeaderSectionProps) {
  return (
    <div className="panel p-6 sm:p-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-subtext">{OUTLINE_COPY.currentOutline}</span>
          <select
            className="select w-auto"
            name="active_outline_id"
            value={props.activeOutlineId}
            onChange={(event) => props.onSwitchOutline(event.target.value)}
          >
            {props.outlines.map((outline) => (
              <option key={outline.id} value={outline.id}>
                {outline.title}
                {outline.has_chapters ? "（已有章节）" : ""}
              </option>
            ))}
          </select>

          <button
            className={props.outlines.length === 0 ? "btn btn-primary" : "btn btn-secondary"}
            onClick={props.onOpenCreate}
            type="button"
          >
            {OUTLINE_COPY.create}
          </button>

          <button
            className="btn btn-secondary"
            disabled={!props.activeOutlineId}
            onClick={props.onOpenRename}
            type="button"
          >
            {OUTLINE_COPY.rename}
          </button>

          <button
            className="btn btn-ghost text-danger hover:bg-danger/10"
            disabled={!props.activeOutlineId}
            onClick={props.onDelete}
            type="button"
          >
            {OUTLINE_COPY.delete}
          </button>
        </div>
        <div className="text-xs text-subtext">
          {props.activeOutlineHasChapters ? OUTLINE_COPY.hasChapters : OUTLINE_COPY.noChapters}
        </div>
      </div>
    </div>
  );
}

export type OutlineActionsBarProps = {
  canCreateChapters: boolean;
  createChaptersDisabledReason?: string;
  dirty: boolean;
  saving: boolean;
  importing: boolean;
  onCreateChapters: () => void;
  onImportOutlineFile: (file: File) => void;
  onOpenGenerate: () => void;
  onSave: () => void;
};

export function OutlineActionsBar(props: OutlineActionsBarProps) {
  const importInputRef = useRef<HTMLInputElement | null>(null);

  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex items-center gap-2">
        <button
          className={props.canCreateChapters ? "btn btn-primary" : "btn btn-secondary"}
          disabled={!props.canCreateChapters}
          onClick={props.onCreateChapters}
          title={props.canCreateChapters ? undefined : props.createChaptersDisabledReason}
          type="button"
        >
          {OUTLINE_COPY.createChapters}
        </button>
        <button className="btn btn-secondary" onClick={props.onOpenGenerate} type="button">
          {OUTLINE_COPY.generate}
        </button>
        <button
          className="btn btn-secondary"
          disabled={props.saving || props.importing}
          onClick={() => importInputRef.current?.click()}
          type="button"
        >
          {props.importing ? OUTLINE_COPY.importingOutlineFile : OUTLINE_COPY.importOutlineFile}
        </button>
        <input
          ref={importInputRef}
          accept=".md,.markdown,.txt,text/markdown,text/plain"
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) props.onImportOutlineFile(file);
            event.currentTarget.value = "";
          }}
          type="file"
        />
      </div>
      <button
        className={props.dirty ? "btn btn-primary" : "btn btn-secondary"}
        disabled={!props.dirty || props.saving || props.importing}
        onClick={props.onSave}
        type="button"
      >
        {OUTLINE_COPY.save}
      </button>
    </div>
  );
}

export function OutlineGuideSection() {
  return (
    <div className="panel p-6 sm:p-8">
      <div className="text-sm text-ink">{OUTLINE_COPY.flowTitle}</div>
      <div className="mt-1 text-xs text-subtext">{OUTLINE_COPY.flowDescription}</div>
      <div className="mt-1 text-[11px] text-subtext">{OUTLINE_COPY.flowHint}</div>
    </div>
  );
}

export type OutlineEditorSectionProps = {
  content: string;
  onChange: (value: string) => void;
};

export function OutlineEditorSection(props: OutlineEditorSectionProps) {
  return (
    <>
      <MarkdownEditor
        value={props.content}
        onChange={props.onChange}
        placeholder={OUTLINE_COPY.editorPlaceholder}
        minRows={16}
        name="outline_content_md"
      />
      <div className="text-xs text-subtext">{OUTLINE_COPY.hotkeyHint}</div>
    </>
  );
}

export type OutlineTitleModalProps = {
  open: boolean;
  mode: "create" | "rename";
  title: string;
  onTitleChange: (value: string) => void;
  onClose: () => void;
  onConfirm: () => void;
};

export function OutlineTitleModal(props: OutlineTitleModalProps) {
  const label = getOutlineTitleModalLabel(props.mode);
  return (
    <Modal open={props.open} onClose={props.onClose} panelClassName="surface max-w-md p-6" ariaLabel={label}>
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="font-content text-2xl">{label}</div>
          <div className="mt-1 text-xs text-subtext">{OUTLINE_COPY.titleModalHint}</div>
        </div>
        <button className="btn btn-secondary" onClick={props.onClose} type="button">
          {OUTLINE_COPY.close}
        </button>
      </div>

      <div className="mt-4 grid gap-3">
        <label className="grid gap-1">
          <span className="text-xs text-subtext">{OUTLINE_COPY.titleLabel}</span>
          <input
            className="input"
            name="outline_title"
            value={props.title}
            onChange={(event) => props.onTitleChange(event.target.value)}
          />
        </label>
      </div>

      <div className="mt-5 flex justify-end gap-2">
        <button className="btn btn-secondary" onClick={props.onClose} type="button">
          {OUTLINE_COPY.cancel}
        </button>
        <button className="btn btn-primary" onClick={props.onConfirm} type="button">
          {OUTLINE_COPY.confirm}
        </button>
      </div>
    </Modal>
  );
}

export type OutlineGenerationModalProps = {
  open: boolean;
  generating: boolean;
  genForm: OutlineGenForm;
  onGenFormChange: (patch: Partial<OutlineGenForm>) => void;
  streamEnabled: boolean;
  onStreamEnabledChange: (value: boolean) => void;
  streamProgress: OutlineStreamProgress | null;
  streamPreviewJson: string;
  streamRawText: string;
  preview: OutlineGenResult | null;
  onClose: () => void;
  onCancelGenerate: () => void;
  onGenerate: () => void;
  onClearPreview: () => void;
  onOverwriteCurrent: () => void;
  onSaveAsNew: () => void;
  onPreviewContentChange: (value: string) => void;
};

export function OutlineGenerationModal(props: OutlineGenerationModalProps) {
  return (
    <Modal
      open={props.open}
      onClose={props.onClose}
      panelClassName="surface max-w-2xl p-6"
      ariaLabel={OUTLINE_COPY.generateTitle}
    >
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="font-content text-2xl">{OUTLINE_COPY.generateTitle}</div>
          <div className="mt-1 text-xs text-subtext">{OUTLINE_COPY.generateHint}</div>
        </div>
        <button className="btn btn-secondary" onClick={props.onClose} type="button">
          {OUTLINE_COPY.close}
        </button>
      </div>

      <div className="mt-4 grid gap-4">
        <div className="rounded-atelier border border-border bg-canvas p-4">
          <div className="text-sm text-ink">{OUTLINE_COPY.generateFormTitle}</div>
          <div className="mt-1 text-xs text-subtext">{OUTLINE_COPY.generateFormHint}</div>
          <div className="mt-3 grid gap-4 sm:grid-cols-3">
            <label className="grid gap-1">
              <span className="text-xs text-subtext">{OUTLINE_COPY.chapterCount}</span>
              <input
                className="input"
                type="number"
                min={1}
                name="chapter_count"
                value={props.genForm.chapter_count}
                onChange={(event) => props.onGenFormChange({ chapter_count: Number(event.target.value) })}
              />
              <div className="text-[11px] text-subtext">{OUTLINE_COPY.chapterCountHint}</div>
            </label>
            <label className="grid gap-1 sm:col-span-2">
              <span className="text-xs text-subtext">{OUTLINE_COPY.tone}</span>
              <input
                className="input"
                name="tone"
                value={props.genForm.tone}
                onChange={(event) => props.onGenFormChange({ tone: event.target.value })}
                placeholder={OUTLINE_COPY.tonePlaceholder}
              />
            </label>
            <label className="grid gap-1 sm:col-span-3">
              <span className="text-xs text-subtext">{OUTLINE_COPY.pacing}</span>
              <input
                className="input"
                name="pacing"
                value={props.genForm.pacing}
                onChange={(event) => props.onGenFormChange({ pacing: event.target.value })}
                placeholder={OUTLINE_COPY.pacingPlaceholder}
              />
            </label>
          </div>
        </div>

        <div className="rounded-atelier border border-border bg-canvas p-4">
          <div className="text-sm text-ink">{OUTLINE_COPY.advancedTitle}</div>
          <div className="mt-1 text-xs text-subtext">{OUTLINE_COPY.advancedHint}</div>
          <div className="mt-3 grid gap-3 sm:grid-cols-3">
            <label className="flex items-center gap-2 text-sm text-ink">
              <input
                className="checkbox"
                checked={props.genForm.include_world_setting}
                name="include_world_setting"
                onChange={(event) => props.onGenFormChange({ include_world_setting: event.target.checked })}
                type="checkbox"
              />
              {OUTLINE_COPY.includeWorldSetting}
            </label>
            <label className="flex items-center gap-2 text-sm text-ink">
              <input
                className="checkbox"
                checked={props.genForm.include_characters}
                name="include_characters"
                onChange={(event) => props.onGenFormChange({ include_characters: event.target.checked })}
                type="checkbox"
              />
              {OUTLINE_COPY.includeCharacters}
            </label>
            <label className="flex items-center gap-2 text-sm text-ink sm:col-span-3">
              <input
                className="checkbox"
                checked={props.streamEnabled}
                name="stream"
                onChange={(event) => props.onStreamEnabledChange(event.target.checked)}
                type="checkbox"
              />
              {OUTLINE_COPY.stream}
            </label>
          </div>
        </div>
      </div>

      {props.streamEnabled ? (
        <div className="mt-4 grid gap-3">
          {props.streamProgress ? (
            <div className="panel p-3">
              <div className="flex items-center justify-between gap-2 text-xs text-subtext">
                <span className="truncate">{props.streamProgress.message}</span>
                <span className="shrink-0">{props.streamProgress.progress}%</span>
              </div>
              <ProgressBar ariaLabel="大纲流式生成进度" value={props.streamProgress.progress} />
            </div>
          ) : null}

          {props.streamPreviewJson ? (
            <details className="panel p-3" open>
              <summary className="ui-transition-fast cursor-pointer text-xs text-subtext hover:text-ink">
                {OUTLINE_COPY.streamPreviewTitle}
                {props.preview ? ` · 已解析 ${props.preview.chapters.length} 章` : ""}
              </summary>
              <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words text-xs text-ink">
                {props.streamPreviewJson}
              </pre>
            </details>
          ) : props.generating ? (
            <div className="panel p-3 text-xs text-subtext">{OUTLINE_COPY.streamPreviewWaiting}</div>
          ) : null}

          {props.streamRawText ? (
            <details className="panel p-3" open={props.generating}>
              <summary className="ui-transition-fast cursor-pointer text-xs text-subtext hover:text-ink">
                {OUTLINE_COPY.streamRawTitle}
              </summary>
              <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words text-xs text-ink">
                {props.streamRawText}
              </pre>
            </details>
          ) : props.generating ? (
            <div className="panel p-3 text-xs text-subtext">{OUTLINE_COPY.streamRawWaiting}</div>
          ) : null}
        </div>
      ) : null}

      <div className="mt-5 text-xs text-subtext">{OUTLINE_COPY.riskHint}</div>
      <div className="mt-5 flex justify-end gap-2">
        <button className="btn btn-secondary" onClick={props.onClose} type="button">
          {OUTLINE_COPY.cancel}
        </button>
        {props.streamEnabled && (props.generating || props.streamProgress?.status === "processing") ? (
          <button className="btn btn-secondary" onClick={props.onCancelGenerate} type="button">
            {OUTLINE_COPY.cancelGenerate}
          </button>
        ) : null}
        <button className="btn btn-primary" disabled={props.generating} onClick={props.onGenerate} type="button">
          {props.generating ? OUTLINE_COPY.generatingButton : OUTLINE_COPY.generateButton}
        </button>
      </div>

      {props.preview ? (
        <div className="panel mt-6 p-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-sm text-ink">{OUTLINE_COPY.previewTitle}</div>
              <div className="mt-1 text-xs text-subtext">
                {getOutlinePreviewMetaText(props.preview.chapters.length, props.preview.parse_error?.message)}
              </div>
              <div className="mt-1 text-[11px] text-subtext">{OUTLINE_COPY.previewActionHint}</div>
            </div>
            <div className="flex gap-2">
              <button className="btn btn-secondary" onClick={props.onClearPreview} type="button">
                {OUTLINE_COPY.previewCancel}
              </button>
              <button className="btn btn-secondary" onClick={props.onOverwriteCurrent} type="button">
                {OUTLINE_COPY.overwriteAndSave}
              </button>
              <button className="btn btn-primary" onClick={props.onSaveAsNew} type="button">
                {OUTLINE_COPY.saveAsNew}
              </button>
            </div>
          </div>
          <div className="mt-3">
            <MarkdownEditor
              value={props.preview.outline_md}
              onChange={props.onPreviewContentChange}
              minRows={10}
              name="generated_outline_preview"
            />
          </div>
        </div>
      ) : null}
    </Modal>
  );
}
