import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { GhostwriterIndicator } from "../components/atelier/GhostwriterIndicator";
import { MarkdownEditor } from "../components/atelier/MarkdownEditor";
import { WizardNextBar } from "../components/atelier/WizardNextBar";
import { ToolContent } from "../components/layout/AppShell";
import { Drawer } from "../components/ui/Drawer";
import { ProgressBar } from "../components/ui/ProgressBar";
import { AiGenerateDrawer } from "../components/writing/AiGenerateDrawer";
import { BatchGenerationModal } from "../components/writing/BatchGenerationModal";
import { ChapterListPanel } from "../components/writing/ChapterListPanel";
import { CreateChapterDialog } from "../components/writing/CreateChapterDialog";
import { ChapterAnalysisModal } from "../components/writing/ChapterAnalysisModal";
import { ContentOptimizeCompareDrawer } from "../components/writing/ContentOptimizeCompareDrawer";
import { ContextPreviewDrawer } from "../components/writing/ContextPreviewDrawer";
import { ForeshadowDrawer } from "../components/writing/ForeshadowDrawer";
import { GenerationHistoryDrawer } from "../components/writing/GenerationHistoryDrawer";
import { MemoryUpdateDrawer } from "../components/writing/MemoryUpdateDrawer";
import { PostEditCompareDrawer } from "../components/writing/PostEditCompareDrawer";
import { PromptInspectorDrawer } from "../components/writing/PromptInspectorDrawer";
import { TablesPanel } from "../components/writing/TablesPanel";
import { WritingToolbar } from "../components/writing/WritingToolbar";
import { useConfirm } from "../components/ui/confirm";
import { useToast } from "../components/ui/toast";
import { usePersistentOutletIsActive } from "../hooks/usePersistentOutlet";
import { useProjectData } from "../hooks/useProjectData";
import { useWizardProgress } from "../hooks/useWizardProgress";
import { UnsavedChangesGuard } from "../hooks/useUnsavedChangesGuard";
import { ApiError, apiJson } from "../services/apiClient";
import { getWizardProjectChangedAt } from "../services/wizard";
import { useApplyGenerationRun } from "./writing/useApplyGenerationRun";
import { useBatchGeneration } from "./writing/useBatchGeneration";
import { useChapterAnalysis } from "./writing/useChapterAnalysis";
import { useChapterCrud } from "./writing/useChapterCrud";
import { useChapterEditor } from "./writing/useChapterEditor";
import { useChapterGeneration } from "./writing/useChapterGeneration";
import { useGenerationHistory } from "./writing/useGenerationHistory";
import { useOutlineSwitcher } from "./writing/useOutlineSwitcher";
import { humanizeChapterStatus } from "../lib/humanize";
import type { ChapterStatus, Character, LLMPreset, Outline, OutlineListItem } from "../types";

type WritingLoaded = { outlines: OutlineListItem[]; outline: Outline; preset: LLMPreset; characters: Character[] };

const CHAPTER_LIST_SIDEBAR_WIDTH_CLASS = "w-[260px]" as const;

type ChapterAutoUpdatesTriggerResult = {
  tasks: Record<string, string | null>;
  chapter_token: string | null;
};

function pickFirstProjectTaskId(tasks: Record<string, string | null> | null | undefined): string | null {
  if (!tasks) return null;
  for (const v of Object.values(tasks)) {
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return null;
}

export function WritingPage() {
  const { projectId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedChapterId = searchParams.get("chapterId");
  const applyRunId = searchParams.get("applyRunId");
  const navigate = useNavigate();
  const toast = useToast();
  const confirm = useConfirm();
  const outletActive = usePersistentOutletIsActive();
  const wizard = useWizardProgress(projectId);
  const refreshWizard = wizard.refresh;
  const bumpWizardLocal = wizard.bumpLocal;
  const lastProjectChangedAtRef = useRef<string | null>(null);

  const [chapterListOpen, setChapterListOpen] = useState(false);
  const writingQuery = useProjectData<WritingLoaded>(projectId, async (id) => {
    const [outlineRes, presetRes, charactersRes] = await Promise.all([
      apiJson<{ outline: Outline }>(`/api/projects/${id}/outline`),
      apiJson<{ llm_preset: LLMPreset }>(`/api/projects/${id}/llm_preset`),
      apiJson<{ characters: Character[] }>(`/api/projects/${id}/characters`),
    ]);
    const outlinesRes = await apiJson<{ outlines: OutlineListItem[] }>(`/api/projects/${id}/outlines`);
    return {
      outlines: outlinesRes.data.outlines,
      outline: outlineRes.data.outline,
      preset: presetRes.data.llm_preset,
      characters: charactersRes.data.characters,
    };
  });
  const outlines = writingQuery.data?.outlines ?? [];
  const outline = writingQuery.data?.outline ?? null;
  const characters = writingQuery.data?.characters ?? [];
  const preset = writingQuery.data?.preset ?? null;
  const refreshWriting = writingQuery.refresh;

  const chapterEditor = useChapterEditor({
    projectId,
    requestedChapterId,
    searchParams,
    setSearchParams,
    toast,
    confirm,
    refreshWizard,
    bumpWizardLocal,
  });
  const {
    loading,
    chapters,
    setChapters,
    refreshChapters,
    activeId,
    setActiveId,
    activeChapter,
    baseline,
    form,
    setForm,
    dirty,
    saveChapter,
    requestSelectChapter: requestSelectChapterBase,
    loadingChapter,
    saving,
  } = chapterEditor;
  const contentTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const [contentEditorTab, setContentEditorTab] = useState<"edit" | "preview">("edit");

  useEffect(() => {
    if (!projectId) {
      lastProjectChangedAtRef.current = null;
      return;
    }
    lastProjectChangedAtRef.current = getWizardProjectChangedAt(projectId);
  }, [projectId]);

  useEffect(() => {
    if (!projectId) return;
    if (!outletActive) return;
    if (dirty) return;

    const changedAt = getWizardProjectChangedAt(projectId);
    if ((changedAt ?? null) === (lastProjectChangedAtRef.current ?? null)) return;
    lastProjectChangedAtRef.current = changedAt;

    void refreshWriting();
    void refreshChapters();
    void refreshWizard();
  }, [dirty, outletActive, projectId, refreshChapters, refreshWriting, refreshWizard]);

  const [aiOpen, setAiOpen] = useState(false);
  const [promptInspectorOpen, setPromptInspectorOpen] = useState(false);
  const [postEditCompareOpen, setPostEditCompareOpen] = useState(false);
  const [contentOptimizeCompareOpen, setContentOptimizeCompareOpen] = useState(false);
  const [tablesOpen, setTablesOpen] = useState(false);
  const [contextPreviewOpen, setContextPreviewOpen] = useState(false);
  const [memoryUpdateOpen, setMemoryUpdateOpen] = useState(false);
  const [foreshadowOpen, setForeshadowOpen] = useState(false);
  const [autoUpdatesTriggering, setAutoUpdatesTriggering] = useState(false);
  const autoGenerateNextRef = useRef<{ chapterId: string; mode: "replace" | "append" } | null>(null);
  const isDoneReadonly = Boolean(baseline && form && baseline.status === "done" && form.status === "done");

  useEffect(() => {
    if (!activeChapter) autoGenerateNextRef.current = null;
  }, [activeChapter]);

  useApplyGenerationRun({
    applyRunId,
    activeChapter,
    form,
    dirty,
    confirm,
    toast,
    saveChapter,
    searchParams,
    setSearchParams,
    setForm,
  });

  const requestSelectChapter = useCallback(
    async (id: string) => {
      autoGenerateNextRef.current = null;
      await requestSelectChapterBase(id);
    },
    [requestSelectChapterBase],
  );

  const chapterCrud = useChapterCrud({
    projectId,
    chapters,
    setChapters,
    activeChapter,
    setActiveId,
    refreshChapters,
    requestSelectChapter,
    toast,
    confirm,
    bumpWizardLocal,
    refreshWizard,
  });

  const generation = useChapterGeneration({
    projectId,
    activeChapter,
    chapters,
    form,
    setForm,
    preset,
    dirty,
    saveChapter,
    requestSelectChapter,
    toast,
    confirm,
  });
  const {
    generating,
    genRequestId,
    genStreamProgress,
    genForm,
    setGenForm,
    postEditCompare,
    applyPostEditVariant,
    contentOptimizeCompare,
    applyContentOptimizeVariant,
    generate,
    abortGenerate,
  } = generation;

  const batch = useBatchGeneration({
    projectId,
    preset,
    activeChapter,
    chapters,
    genForm,
    searchParams,
    setSearchParams,
    requestSelectChapter,
    toast,
  });

  const analysis = useChapterAnalysis({ activeChapter, preset, genForm, form, setForm, dirty, saveChapter, toast });
  const history = useGenerationHistory({ projectId, toast });

  const activeOutlineId = outline?.id ?? "";
  const switchOutline = useOutlineSwitcher({
    projectId,
    activeOutlineId,
    dirty,
    confirm,
    toast,
    saveChapter,
    bumpWizardLocal,
    refreshWizard,
    refreshChapters,
    refreshWriting,
  });

  const locateInEditor = useCallback(
    (excerpt: string) => {
      if (!excerpt || !form) return;
      const needleRaw = excerpt.trim();
      if (!needleRaw) return;

      const haystack = form.content_md ?? "";
      let needle = needleRaw;
      let index = haystack.indexOf(needle);
      if (index < 0 && needle.length > 20) {
        needle = needle.slice(0, 20);
        index = haystack.indexOf(needle);
      }
      if (index < 0) {
        toast.toastError("未在正文中找到该引用片段（可复制后 Ctrl/Cmd+F 搜索）");
        return;
      }

      setContentEditorTab("edit");
      window.requestAnimationFrame(() => {
        const el = contentTextareaRef.current;
        if (!el) return;
        el.focus();
        el.setSelectionRange(index, Math.min(haystack.length, index + needle.length));
      });
    },
    [contentTextareaRef, form, setContentEditorTab, toast],
  );

  const saveAndTriggerAutoUpdates = useCallback(async () => {
    if (!projectId || !activeChapter) return;
    if (autoUpdatesTriggering) return;
    if (!dirty) return;

    setAutoUpdatesTriggering(true);
    try {
      const ok = await saveChapter({ silent: true });
      if (!ok) return;

      const res = await apiJson<ChapterAutoUpdatesTriggerResult>(
        `/api/chapters/${activeChapter.id}/trigger_auto_updates`,
        {
          method: "POST",
          body: JSON.stringify({}),
        },
      );
      const taskId = pickFirstProjectTaskId(res.data.tasks);
      toast.toastSuccess(
        "已保存并创建无感更新任务",
        res.request_id,
        taskId
          ? {
              label: "打开 TaskCenter",
              onClick: () => navigate(`/projects/${projectId}/tasks?project_task_id=${encodeURIComponent(taskId)}`),
            }
          : undefined,
      );
    } catch (e) {
      const err =
        e instanceof ApiError
          ? e
          : new ApiError({ code: "UNKNOWN", message: String(e), requestId: "unknown", status: 0 });
      toast.toastError(`${err.message} (${err.code})`, err.requestId);
    } finally {
      setAutoUpdatesTriggering(false);
    }
  }, [activeChapter, autoUpdatesTriggering, dirty, projectId, saveChapter, toast, navigate]);

  const saveAndGenerateNext = useCallback(async () => {
    if (!activeChapter) return;

    const ok = await saveChapter();
    if (!ok) return;

    const sorted = [...chapters].sort((a, b) => (a.number ?? 0) - (b.number ?? 0));
    const idx = sorted.findIndex((c) => c.id === activeChapter.id);
    const next =
      idx >= 0
        ? (sorted[idx + 1] ?? null)
        : (sorted.find((c) => (c.number ?? 0) > (activeChapter.number ?? 0)) ?? null);

    if (!next) {
      toast.toastSuccess("已保存，已是最后一章");
      return;
    }

    const nextHasContent = Boolean((next.content_md ?? "").trim() || (next.summary ?? "").trim());
    if (nextHasContent) {
      const replaceOk = await confirm.confirm({
        title: `下一章（第 ${next.number} 章）已有内容，仍要开始生成？`,
        description: "将以“替换”模式生成草稿（生成结果不会自动保存）。",
        confirmText: "继续",
        cancelText: "取消",
        danger: true,
      });
      if (!replaceOk) return;
    }

    autoGenerateNextRef.current = { chapterId: next.id, mode: "replace" };
    setActiveId(next.id);
    setAiOpen(true);
  }, [activeChapter, chapters, confirm, saveChapter, setActiveId, setAiOpen, toast]);

  useEffect(() => {
    const pending = autoGenerateNextRef.current;
    if (!pending) return;
    if (!activeChapter || !form) return;
    if (activeChapter.id !== pending.chapterId) return;
    if (generating) return;
    autoGenerateNextRef.current = null;
    void generate(pending.mode);
  }, [activeChapter, form, generate, generating]);

  if (loading) return <ToolContent className="text-subtext">加载中...</ToolContent>;

  return (
    <ToolContent className="grid gap-4 pb-24">
      {dirty && outletActive ? <UnsavedChangesGuard when={dirty} /> : null}
      <WritingToolbar
        outlines={outlines}
        activeOutlineId={activeOutlineId}
        chaptersCount={chapters.length}
        batchProgressText={
          batch.batchTask && (batch.batchTask.status === "queued" || batch.batchTask.status === "running")
            ? `（${batch.batchTask.completed_count}/${batch.batchTask.total_count}）`
            : ""
        }
        aiGenerateDisabled={!activeChapter || loadingChapter}
        onSwitchOutline={(outlineId) => void switchOutline(outlineId)}
        onOpenChapterList={() => setChapterListOpen(true)}
        onOpenBatch={batch.openModal}
        onOpenHistory={history.openDrawer}
        onOpenAiGenerate={() => setAiOpen(true)}
        onOpenMemoryUpdate={() => {
          if (!activeChapter) return;
          if (dirty) {
            toast.toastWarning("请先保存当前章节后再进行记忆更新。");
            return;
          }
          if (activeChapter.status !== "done") {
            toast.toastWarning(
              `仅状态为 ${humanizeChapterStatus("done")} 的章节允许记忆更新；请先将章节标记为 ${humanizeChapterStatus("done")}。`,
            );
            return;
          }
          setMemoryUpdateOpen(true);
        }}
        onOpenTaskCenter={() => {
          if (!projectId) return;
          const qs = new URLSearchParams();
          if (activeId) qs.set("chapterId", activeId);
          navigate(`/projects/${projectId}/tasks${qs.toString() ? `?${qs.toString()}` : ""}`);
        }}
        onOpenForeshadow={() => setForeshadowOpen(true)}
        onOpenTables={() => setTablesOpen(true)}
        onOpenContextPreview={() => setContextPreviewOpen(true)}
        onCreateChapter={chapterCrud.openCreate}
      />

      <div className="flex gap-4">
        <aside className={`hidden ${CHAPTER_LIST_SIDEBAR_WIDTH_CLASS} shrink-0 lg:block`}>
          <ChapterListPanel
            chapters={chapters}
            activeId={activeId}
            onSelectChapter={(chapterId) => void requestSelectChapter(chapterId)}
          />
        </aside>

        <section className="min-w-0 flex-1">
          {!activeChapter || !form ? (
            <div className="mx-auto w-full max-w-4xl rounded-atelier border border-border bg-surface p-8 text-sm text-subtext shadow-sm">
              请选择或新建章节开始写作。
            </div>
          ) : (
            <div className="mx-auto w-full max-w-4xl rounded-atelier border border-border bg-surface p-5 shadow-sm">
              {isDoneReadonly ? (
                <div className="callout-warning mb-4 flex flex-wrap items-center justify-between gap-2">
                  <div className="text-xs">
                    本章已定稿：为避免误操作，编辑区默认只读。如需修改，请先回退为 {humanizeChapterStatus("drafting")}。
                  </div>
                  <button
                    className="btn btn-secondary"
                    onClick={() => setForm((v) => (v ? { ...v, status: "drafting" } : v))}
                    type="button"
                  >
                    回退为 {humanizeChapterStatus("drafting")} 并编辑
                  </button>
                </div>
              ) : null}
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="font-content text-2xl text-ink">
                    第 {activeChapter.number} 章 <span className="text-subtext">{dirty ? "（未保存）" : ""}</span>
                  </div>
                  <div className="mt-1 text-xs text-subtext">updated_at: {activeChapter.updated_at}</div>
                </div>
                <div className="flex flex-wrap items-center justify-end gap-2">
                  <button
                    className="btn btn-secondary"
                    disabled={loadingChapter || generating}
                    onClick={analysis.openModal}
                    type="button"
                  >
                    分析
                  </button>
                  <button
                    className="btn btn-secondary"
                    disabled={!projectId || loadingChapter || generating}
                    onClick={() => {
                      if (!projectId || !activeChapter) return;
                      navigate(`/projects/${projectId}/chapter-analysis?chapterId=${activeChapter.id}`);
                    }}
                    type="button"
                  >
                    标注回溯
                  </button>
                  <button
                    className="btn btn-ghost text-accent hover:bg-accent/10"
                    disabled={loadingChapter || generating}
                    onClick={() => void chapterCrud.deleteChapter()}
                    type="button"
                  >
                    删除
                  </button>
                  <button
                    className="btn btn-secondary"
                    disabled={!dirty || saving || loadingChapter || generating || autoUpdatesTriggering}
                    onClick={() => void saveAndTriggerAutoUpdates()}
                    type="button"
                  >
                    {autoUpdatesTriggering ? "保存并触发中..." : "一键保存并触发更新"}
                  </button>
                  <button
                    className="btn btn-primary"
                    disabled={!dirty || saving || loadingChapter || generating}
                    onClick={() => void saveChapter()}
                    type="button"
                  >
                    {saving ? "保存中..." : "保存"}
                  </button>
                </div>
              </div>

              {generating ? (
                <GhostwriterIndicator
                  className="mt-4"
                  label={
                    genForm.stream && genStreamProgress
                      ? `${genStreamProgress.message}（${genStreamProgress.progress}%）`
                      : "墨迹渗入纸张中…生成需要一点时间"
                  }
                />
              ) : null}

              <div className="mt-4 grid gap-3 sm:grid-cols-3">
                <label className="grid gap-1 sm:col-span-2">
                  <span className="text-xs text-subtext">标题</span>
                  <input
                    className="input-underline font-content text-xl"
                    name="title"
                    value={form.title}
                    readOnly={isDoneReadonly}
                    onChange={(e) => {
                      setForm((v) => (v ? { ...v, title: e.target.value } : v));
                    }}
                  />
                </label>
                <label className="grid gap-1 sm:col-span-1">
                  <span className="text-xs text-subtext">状态</span>
                  <select
                    className="select"
                    name="status"
                    value={form.status}
                    onChange={(e) => {
                      const next = e.target.value as ChapterStatus;
                      setForm((v) => (v ? { ...v, status: next } : v));
                    }}
                  >
                    <option value="planned">{humanizeChapterStatus("planned")}</option>
                    <option value="drafting">{humanizeChapterStatus("drafting")}</option>
                    <option value="done">{humanizeChapterStatus("done")}</option>
                  </select>
                  <div className="text-[11px] text-subtext">
                    提示：保存不等于定稿。仅状态为 {humanizeChapterStatus("done")} 的章节允许进行记忆更新（Memory
                    Update）写入长期记忆； 定稿章默认只读，修改请先切回 {humanizeChapterStatus("drafting")}。
                  </div>
                </label>
              </div>

              <div className="mt-4 grid gap-3">
                <label className="grid gap-1">
                  <span className="text-xs text-subtext">本章要点</span>
                  <textarea
                    className="textarea atelier-content"
                    name="plan"
                    rows={4}
                    value={form.plan}
                    readOnly={isDoneReadonly}
                    onChange={(e) => {
                      setForm((v) => (v ? { ...v, plan: e.target.value } : v));
                    }}
                  />
                </label>
                <label className="grid gap-1">
                  <span className="text-xs text-subtext">正文（Markdown）</span>
                  <MarkdownEditor
                    value={form.content_md}
                    onChange={(next) => {
                      setForm((v) => (v ? { ...v, content_md: next } : v));
                    }}
                    placeholder="开始写作..."
                    minRows={16}
                    name="content_md"
                    readOnly={isDoneReadonly}
                    tab={contentEditorTab}
                    onTabChange={setContentEditorTab}
                    textareaRef={(el) => {
                      contentTextareaRef.current = el;
                    }}
                  />
                </label>
                <label className="grid gap-1">
                  <span className="text-xs text-subtext">摘要（可选）</span>
                  <textarea
                    className="textarea atelier-content"
                    name="summary"
                    rows={3}
                    value={form.summary}
                    readOnly={isDoneReadonly}
                    onChange={(e) => {
                      setForm((v) => (v ? { ...v, summary: e.target.value } : v));
                    }}
                  />
                </label>
              </div>

              <div className="mt-4 text-xs text-subtext">快捷键：Ctrl/Cmd + S 保存</div>
            </div>
          )}
        </section>
      </div>

      <CreateChapterDialog
        open={chapterCrud.createOpen}
        saving={chapterCrud.createSaving}
        form={chapterCrud.createForm}
        setForm={chapterCrud.setCreateForm}
        onClose={() => chapterCrud.setCreateOpen(false)}
        onSubmit={() => void chapterCrud.createChapter()}
      />

      <BatchGenerationModal
        open={batch.open}
        batchLoading={batch.batchLoading}
        activeChapterNumber={activeChapter?.number ?? null}
        batchCount={batch.batchCount}
        setBatchCount={batch.setBatchCount}
        batchIncludeExisting={batch.batchIncludeExisting}
        setBatchIncludeExisting={batch.setBatchIncludeExisting}
        batchTask={batch.batchTask}
        batchItems={batch.batchItems}
        onClose={batch.closeModal}
        onCancelTask={() => void batch.cancelBatchGeneration()}
        onStartTask={() => void batch.startBatchGeneration()}
        onApplyItemToEditor={(it) => void batch.applyBatchItemToEditor(it)}
      />

      <ChapterAnalysisModal
        open={analysis.open}
        analysisLoading={analysis.analysisLoading}
        rewriteLoading={analysis.rewriteLoading}
        applyLoading={analysis.applyLoading}
        analysisFocus={analysis.analysisFocus}
        setAnalysisFocus={analysis.setAnalysisFocus}
        analysisResult={analysis.analysisResult}
        rewriteInstruction={analysis.rewriteInstruction}
        setRewriteInstruction={analysis.setRewriteInstruction}
        onClose={analysis.closeModal}
        onAnalyze={() => void analysis.analyzeChapter()}
        onApplyAnalysisToMemory={() => void analysis.applyAnalysisToMemory()}
        onLocateInEditor={locateInEditor}
        onRewriteFromAnalysis={() => void analysis.rewriteFromAnalysis()}
      />

      <Drawer
        open={chapterListOpen}
        onClose={() => setChapterListOpen(false)}
        side="left"
        overlayClassName="lg:hidden"
        ariaLabel="章节列表"
        panelClassName={`h-full ${CHAPTER_LIST_SIDEBAR_WIDTH_CLASS} overflow-hidden border-r border-border bg-surface shadow-sm`}
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div className="text-sm text-ink">章节列表</div>
          <button className="btn btn-secondary" onClick={() => setChapterListOpen(false)} type="button">
            关闭
          </button>
        </div>

        <div className="h-full overflow-auto p-2">
          <ChapterListPanel
            chapters={chapters}
            activeId={activeId}
            containerClassName=""
            onSelectChapter={(chapterId) => {
              setChapterListOpen(false);
              void requestSelectChapter(chapterId);
            }}
          />
        </div>
      </Drawer>

      <AiGenerateDrawer
        open={aiOpen}
        generating={generating}
        preset={preset}
        projectId={projectId}
        activeChapter={Boolean(activeChapter)}
        dirty={dirty}
        saving={saving || loadingChapter}
        genForm={genForm}
        setGenForm={setGenForm}
        characters={characters}
        streamProgress={genStreamProgress}
        onClose={() => setAiOpen(false)}
        onSave={() => void saveChapter()}
        onSaveAndGenerateNext={() => void saveAndGenerateNext()}
        onGenerateAppend={() => void generate("append")}
        onGenerateReplace={() => void generate("replace")}
        onCancelGenerate={abortGenerate}
        onOpenPromptInspector={() => setPromptInspectorOpen(true)}
        postEditCompareAvailable={Boolean(postEditCompare)}
        onOpenPostEditCompare={() => setPostEditCompareOpen(true)}
        contentOptimizeCompareAvailable={Boolean(contentOptimizeCompare)}
        onOpenContentOptimizeCompare={() => setContentOptimizeCompareOpen(true)}
      />

      <PostEditCompareDrawer
        open={postEditCompareOpen && Boolean(postEditCompare)}
        onClose={() => setPostEditCompareOpen(false)}
        rawContentMd={postEditCompare?.rawContentMd ?? ""}
        editedContentMd={postEditCompare?.editedContentMd ?? ""}
        requestId={postEditCompare?.requestId ?? null}
        appliedChoice={postEditCompare?.appliedChoice ?? "post_edit"}
        onApplyRaw={() => void applyPostEditVariant("raw")}
        onApplyPostEdit={() => void applyPostEditVariant("post_edit")}
      />

      <ContentOptimizeCompareDrawer
        open={contentOptimizeCompareOpen && Boolean(contentOptimizeCompare)}
        onClose={() => setContentOptimizeCompareOpen(false)}
        rawContentMd={contentOptimizeCompare?.rawContentMd ?? ""}
        optimizedContentMd={contentOptimizeCompare?.optimizedContentMd ?? ""}
        requestId={contentOptimizeCompare?.requestId ?? null}
        appliedChoice={contentOptimizeCompare?.appliedChoice ?? "content_optimize"}
        onApplyRaw={() => void applyContentOptimizeVariant("raw")}
        onApplyOptimized={() => void applyContentOptimizeVariant("content_optimize")}
      />

      <PromptInspectorDrawer
        open={promptInspectorOpen}
        onClose={() => setPromptInspectorOpen(false)}
        preset={preset}
        chapterId={activeChapter?.id ?? undefined}
        draftContentMd={form?.content_md ?? ""}
        generating={generating}
        genForm={genForm}
        setGenForm={setGenForm}
        onGenerate={generate}
      />

      <ContextPreviewDrawer
        open={contextPreviewOpen}
        onClose={() => setContextPreviewOpen(false)}
        projectId={projectId}
        memoryInjectionEnabled={genForm.memory_injection_enabled}
        genInstruction={genForm.instruction}
        genChapterPlan={activeChapter?.plan ?? ""}
        genMemoryQueryText={genForm.memory_query_text}
        genMemoryModules={genForm.memory_modules}
        onChangeMemoryInjectionEnabled={(enabled) =>
          setGenForm((v) => ({ ...v, memory_injection_enabled: Boolean(enabled) }))
        }
      />

      <TablesPanel open={tablesOpen} onClose={() => setTablesOpen(false)} projectId={projectId} />

      <MemoryUpdateDrawer
        open={memoryUpdateOpen}
        onClose={() => setMemoryUpdateOpen(false)}
        projectId={projectId}
        chapterId={activeId ?? undefined}
      />

      <ForeshadowDrawer
        open={foreshadowOpen}
        onClose={() => setForeshadowOpen(false)}
        projectId={projectId}
        activeChapterId={activeId ?? undefined}
      />

      {generating && genForm.stream && !aiOpen ? (
        <div className="fixed inset-x-4 bottom-24 z-40 flex justify-center sm:inset-auto sm:bottom-8 sm:right-8 sm:justify-end">
          <div className="w-full max-w-sm rounded-atelier border border-border bg-surface/90 p-3 shadow-sm backdrop-blur">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm text-ink">AI 流式生成中</div>
                <div className="mt-1 truncate text-xs text-subtext">{genStreamProgress?.message ?? "处理中..."}</div>
                {genRequestId ? (
                  <div className="mt-1 truncate text-[11px] text-subtext">request_id: {genRequestId}</div>
                ) : null}
              </div>
              {genStreamProgress ? (
                <div className="shrink-0 text-xs text-subtext">
                  {Math.max(0, Math.min(100, genStreamProgress.progress))}%
                </div>
              ) : null}
            </div>
            <ProgressBar ariaLabel="写作页流式生成进度" className="mt-2" value={genStreamProgress?.progress ?? 0} />
            <div className="mt-3 flex justify-end gap-2">
              <button className="btn btn-secondary" onClick={() => setAiOpen(true)} type="button">
                展开
              </button>
              <button className="btn btn-secondary" onClick={abortGenerate} type="button">
                取消
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <GenerationHistoryDrawer
        open={history.open}
        onClose={history.closeDrawer}
        loading={history.runsLoading}
        runs={history.runs}
        selectedRun={history.selectedRun}
        onSelectRun={(run) => void history.selectRun(run)}
      />

      <WizardNextBar
        projectId={projectId}
        currentStep="writing"
        progress={wizard.progress}
        loading={wizard.loading}
        dirty={dirty}
        saving={saving || loadingChapter || generating}
        onSave={saveChapter}
      />
    </ToolContent>
  );
}
