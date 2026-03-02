import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { MarkdownEditor } from "../components/atelier/MarkdownEditor";
import { WizardNextBar } from "../components/atelier/WizardNextBar";
import { Modal } from "../components/ui/Modal";
import { useConfirm } from "../components/ui/confirm";
import { useToast } from "../components/ui/toast";
import { useProjectData } from "../hooks/useProjectData";
import { useAutoSave } from "../hooks/useAutoSave";
import { usePersistentOutletIsActive } from "../hooks/usePersistentOutlet";
import { useSaveHotkey } from "../hooks/useSaveHotkey";
import { UnsavedChangesGuard } from "../hooks/useUnsavedChangesGuard";
import { useWizardProgress } from "../hooks/useWizardProgress";
import { ApiError, apiJson } from "../services/apiClient";
import { SSEError, SSEPostClient } from "../services/sseClient";
import { markWizardProjectChanged } from "../services/wizard";
import type { Chapter, LLMPreset, Outline, OutlineListItem, Project } from "../types";

type OutlineGenChapter = { number: number; title: string; beats: string[] };
type OutlineGenResult = {
  outline_md: string;
  chapters: OutlineGenChapter[];
  raw_output: string;
  parse_error?: { code: string; message: string };
};

type OutlineGenForm = {
  chapter_count: number;
  tone: string;
  pacing: string;
  include_world_setting: boolean;
  include_characters: boolean;
};

type OutlineLoaded = { outlines: OutlineListItem[]; outline: Outline; preset: LLMPreset };

function extractOutlineChapters(structure: unknown): OutlineGenChapter[] {
  if (!structure || typeof structure !== "object") return [];
  const maybe = structure as { chapters?: unknown };
  if (!Array.isArray(maybe.chapters)) return [];
  return maybe.chapters
    .map((item) => {
      const raw = item as { number?: unknown; title?: unknown; beats?: unknown };
      const number = typeof raw.number === "number" ? raw.number : Number(raw.number);
      if (!Number.isFinite(number) || number <= 0) return null;
      const title = typeof raw.title === "string" ? raw.title : "";
      const beats = Array.isArray(raw.beats) ? raw.beats.map((b) => String(b)).filter(Boolean) : [];
      return { number, title, beats } satisfies OutlineGenChapter;
    })
    .filter((v): v is OutlineGenChapter => Boolean(v));
}

export function OutlinePage() {
  const { projectId } = useParams();
  const toast = useToast();
  const confirm = useConfirm();
  const navigate = useNavigate();
  const outletActive = usePersistentOutletIsActive();
  const wizard = useWizardProgress(projectId);
  const refreshWizard = wizard.refresh;
  const bumpWizardLocal = wizard.bumpLocal;

  const [saving, setSaving] = useState(false);
  const [generating, setGenerating] = useState(false);

  const [outlines, setOutlines] = useState<OutlineListItem[]>([]);
  const [activeOutline, setActiveOutline] = useState<Outline | null>(null);
  const [preset, setPreset] = useState<LLMPreset | null>(null);
  const [baseline, setBaseline] = useState<string>("");
  const [content, setContent] = useState<string>("");

  const [genModalOpen, setGenModalOpen] = useState(false);
  const [genPreview, setGenPreview] = useState<OutlineGenResult | null>(null);
  const [genStreamEnabled, setGenStreamEnabled] = useState(false);
  const [genStreamProgress, setGenStreamProgress] = useState<{
    message: string;
    progress: number;
    status: string;
  } | null>(null);
  const [genStreamText, setGenStreamText] = useState("");
  const genStreamClientRef = useRef<SSEPostClient | null>(null);
  const genStreamHasChunkRef = useRef(false);
  const wizardRefreshTimerRef = useRef<number | null>(null);
  const savingRef = useRef(false);
  const queuedSaveRef = useRef<{
    nextContent?: string;
    nextStructure?: unknown;
    opts?: { silent?: boolean; snapshotContent?: string };
  } | null>(null);
  const [genForm, setGenForm] = useState<OutlineGenForm>({
    chapter_count: 12,
    tone: "偏现实，克制但有爆点",
    pacing: "前3章强钩子，中段升级，结尾反转",
    include_world_setting: true,
    include_characters: true,
  });

  const [titleModal, setTitleModal] = useState<{ open: boolean; mode: "create" | "rename"; title: string }>({
    open: false,
    mode: "create",
    title: "",
  });

  const outlineQuery = useProjectData<OutlineLoaded>(projectId, async (id) => {
    const [oRes, presetRes] = await Promise.all([
      apiJson<{ outline: Outline }>(`/api/projects/${id}/outline`),
      apiJson<{ llm_preset: LLMPreset }>(`/api/projects/${id}/llm_preset`),
    ]);
    const outlinesRes = await apiJson<{ outlines: OutlineListItem[] }>(`/api/projects/${id}/outlines`);
    return {
      outlines: outlinesRes.data.outlines,
      outline: oRes.data.outline,
      preset: presetRes.data.llm_preset,
    };
  });
  const loading = outlineQuery.loading;

  useEffect(() => {
    if (!outlineQuery.data) return;
    setOutlines(outlineQuery.data.outlines);
    setActiveOutline(outlineQuery.data.outline);
    setPreset(outlineQuery.data.preset);
    const next = outlineQuery.data.outline.content_md ?? "";
    setBaseline(next);
    setContent(next);
  }, [outlineQuery.data]);

  useEffect(() => {
    return () => {
      if (wizardRefreshTimerRef.current !== null) {
        window.clearTimeout(wizardRefreshTimerRef.current);
        wizardRefreshTimerRef.current = null;
      }
    };
  }, []);

  const dirty = content !== baseline;

  const save = useCallback(
    async (
      nextContent?: string,
      nextStructure?: unknown,
      opts?: { silent?: boolean; snapshotContent?: string },
    ): Promise<boolean> => {
      if (!projectId) return false;
      if (savingRef.current) {
        queuedSaveRef.current = { nextContent, nextStructure, opts };
        return false;
      }
      const silent = Boolean(opts?.silent);
      const snapshotContent = opts?.snapshotContent;
      const toSave = snapshotContent ?? nextContent ?? content;
      if (
        nextContent === undefined &&
        snapshotContent === undefined &&
        nextStructure === undefined &&
        toSave === baseline
      )
        return true;

      savingRef.current = true;
      setSaving(true);
      try {
        const scheduleWizardRefresh = () => {
          if (wizardRefreshTimerRef.current !== null) {
            window.clearTimeout(wizardRefreshTimerRef.current);
          }
          wizardRefreshTimerRef.current = window.setTimeout(() => void refreshWizard(), 1200);
        };
        const res = await apiJson<{ outline: Outline }>(`/api/projects/${projectId}/outline`, {
          method: "PUT",
          body: JSON.stringify({ content_md: toSave, structure: nextStructure }),
        });
        const saved = res.data.outline.content_md ?? "";
        setBaseline(saved);
        setContent((prev) => {
          if (nextContent !== undefined) return saved;
          if (prev === toSave) return saved;
          return prev;
        });
        setActiveOutline(res.data.outline);
        markWizardProjectChanged(projectId);
        bumpWizardLocal();
        if (silent) {
          scheduleWizardRefresh();
        } else {
          await refreshWizard();
          toast.toastSuccess("已保存");
        }
        return true;
      } catch (e) {
        const err = e as ApiError;
        toast.toastError(`${err.message} (${err.code})`, err.requestId);
        return false;
      } finally {
        setSaving(false);
        savingRef.current = false;
        if (queuedSaveRef.current) {
          const queued = queuedSaveRef.current;
          queuedSaveRef.current = null;
          void save(queued.nextContent, queued.nextStructure, queued.opts);
        }
      }
    },
    [baseline, bumpWizardLocal, content, projectId, refreshWizard, toast],
  );

  useSaveHotkey(() => void save(), dirty);

  useAutoSave({
    enabled: Boolean(projectId),
    dirty,
    delayMs: 900,
    getSnapshot: () => content,
    onSave: async (snapshot) => {
      await save(undefined, undefined, { silent: true, snapshotContent: snapshot });
    },
    deps: [content, projectId, activeOutline?.id ?? ""],
  });

  const storedChapters = useMemo(() => extractOutlineChapters(activeOutline?.structure), [activeOutline?.structure]);
  const previewChapters = genPreview?.chapters;
  const chaptersForSkeleton = useMemo(
    () => (previewChapters && previewChapters.length > 0 ? previewChapters : storedChapters),
    [previewChapters, storedChapters],
  );
  const canCreateChapters = chaptersForSkeleton.length > 0;

  const createChaptersFromOutline = useCallback(async () => {
    if (!projectId) return;
    if (chaptersForSkeleton.length === 0) return;

    const ok = await confirm.confirm({
      title: "从大纲创建章节骨架？",
      description: `将根据大纲创建 ${chaptersForSkeleton.length} 个章节。`,
      confirmText: "创建",
    });
    if (!ok) return;

    const payload = {
      chapters: chaptersForSkeleton.map((c) => ({
        number: c.number,
        title: c.title,
        plan: (c.beats ?? []).join("；"),
      })),
    };

    try {
      await apiJson<{ chapters: Chapter[] }>(`/api/projects/${projectId}/chapters/bulk_create`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      toast.toastSuccess(`已创建 ${chaptersForSkeleton.length} 个章节`);
      markWizardProjectChanged(projectId);
      bumpWizardLocal();
      navigate(`/projects/${projectId}/writing`);
    } catch (e) {
      const err = e as ApiError;
      if (err.code === "CONFLICT" && err.status === 409) {
        const replaceOk = await confirm.confirm({
          title: "检测到已有章节，是否覆盖？",
          description: "覆盖创建将删除该大纲下所有章节（含正文/摘要），不可恢复。",
          confirmText: "覆盖创建",
          danger: true,
        });
        if (!replaceOk) return;
        try {
          await apiJson<{ chapters: Chapter[] }>(`/api/projects/${projectId}/chapters/bulk_create?replace=true`, {
            method: "POST",
            body: JSON.stringify(payload),
          });
          toast.toastSuccess(`已覆盖创建 ${chaptersForSkeleton.length} 个章节`);
          markWizardProjectChanged(projectId);
          bumpWizardLocal();
          navigate(`/projects/${projectId}/writing`);
        } catch (e2) {
          const err2 = e2 as ApiError;
          toast.toastError(`${err2.message} (${err2.code})`, err2.requestId);
        }
        return;
      }
      toast.toastError(`${err.message} (${err.code})`, err.requestId);
    }
  }, [bumpWizardLocal, chaptersForSkeleton, confirm, navigate, projectId, toast]);

  const activeOutlineId = activeOutline?.id ?? "";

  const refreshOutline = outlineQuery.refresh;

  const switchOutline = useCallback(
    async (nextOutlineId: string) => {
      if (!projectId) return;
      if (!nextOutlineId || nextOutlineId === activeOutlineId) return;

      if (dirty) {
        const choice = await confirm.choose({
          title: "大纲有未保存修改，是否切换？",
          description: "切换后未保存内容会丢失。",
          confirmText: "保存并切换",
          secondaryText: "不保存切换",
          cancelText: "取消",
        });
        if (choice === "cancel") return;
        if (choice === "confirm") {
          const ok = await save();
          if (!ok) return;
        }
      }

      try {
        await apiJson<{ project: Project }>(`/api/projects/${projectId}`, {
          method: "PUT",
          body: JSON.stringify({ active_outline_id: nextOutlineId }),
        });
        markWizardProjectChanged(projectId);
        bumpWizardLocal();
        await refreshOutline();
        await refreshWizard();
        toast.toastSuccess("已切换大纲");
      } catch (e) {
        const err = e as ApiError;
        toast.toastError(`${err.message} (${err.code})`, err.requestId);
      }
    },
    [activeOutlineId, bumpWizardLocal, confirm, dirty, projectId, refreshOutline, refreshWizard, save, toast],
  );

  const createOutline = useCallback(
    async (title: string, contentMd: string, structure: unknown) => {
      if (!projectId) return;
      try {
        await apiJson<{ outline: Outline }>(`/api/projects/${projectId}/outlines`, {
          method: "POST",
          body: JSON.stringify({ title, content_md: contentMd, structure }),
        });
        markWizardProjectChanged(projectId);
        bumpWizardLocal();
        await refreshOutline();
        await refreshWizard();
        toast.toastSuccess("已创建并切换大纲");
      } catch (e) {
        const err = e as ApiError;
        toast.toastError(`${err.message} (${err.code})`, err.requestId);
      }
    },
    [bumpWizardLocal, projectId, refreshOutline, refreshWizard, toast],
  );

  const renameOutline = useCallback(
    async (title: string) => {
      if (!projectId || !activeOutlineId) return;
      try {
        await apiJson<{ outline: Outline }>(`/api/projects/${projectId}/outlines/${activeOutlineId}`, {
          method: "PUT",
          body: JSON.stringify({ title }),
        });
        markWizardProjectChanged(projectId);
        bumpWizardLocal();
        await refreshOutline();
        toast.toastSuccess("已重命名");
      } catch (e) {
        const err = e as ApiError;
        toast.toastError(`${err.message} (${err.code})`, err.requestId);
      }
    },
    [activeOutlineId, bumpWizardLocal, projectId, refreshOutline, toast],
  );

  const deleteOutline = useCallback(async () => {
    if (!projectId || !activeOutlineId) return;
    const ok = await confirm.confirm({
      title: "删除当前大纲？",
      description: "将同时删除该大纲下的章节，且不可恢复。",
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return;
    try {
      await apiJson<Record<string, never>>(`/api/projects/${projectId}/outlines/${activeOutlineId}`, {
        method: "DELETE",
      });
      markWizardProjectChanged(projectId);
      bumpWizardLocal();
      setGenPreview(null);
      await refreshOutline();
      await refreshWizard();
      toast.toastSuccess("已删除大纲");
    } catch (e) {
      const err = e as ApiError;
      toast.toastError(`${err.message} (${err.code})`, err.requestId);
    }
  }, [activeOutlineId, bumpWizardLocal, confirm, projectId, refreshOutline, refreshWizard, toast]);

  const saveGeneratedAsNewOutline = useCallback(async () => {
    if (!projectId || !genPreview) return;

    if (dirty) {
      const choice = await confirm.choose({
        title: "当前大纲有未保存修改，是否继续？",
        description: "保存后再切换可保留修改；不保存继续将丢失未保存内容。",
        confirmText: "保存并继续",
        secondaryText: "不保存继续",
        cancelText: "取消",
      });
      if (choice === "cancel") return;
      if (choice === "confirm") {
        const ok = await save();
        if (!ok) return;
      }
    }

    const title = `AI 大纲 ${new Date().toISOString().slice(0, 16).replace("T", " ")}`;
    setGenModalOpen(false);
    await createOutline(title, genPreview.outline_md, { chapters: genPreview.chapters });
    setGenPreview(null);
  }, [confirm, createOutline, dirty, genPreview, projectId, save]);

  if (loading) return <div className="text-subtext">加载中...</div>;

  return (
    <div className="grid gap-4 pb-[calc(6rem+env(safe-area-inset-bottom))]">
      {dirty && outletActive ? <UnsavedChangesGuard when={dirty} /> : null}
      <div className="panel p-6 sm:p-8">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-subtext">当前大纲</span>
            <select
              className="select w-auto"
              name="active_outline_id"
              value={activeOutlineId}
              onChange={(e) => void switchOutline(e.target.value)}
            >
              {outlines.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.title}
                  {o.has_chapters ? "（已有章节）" : ""}
                </option>
              ))}
            </select>

            <button
              className={outlines.length === 0 ? "btn btn-primary" : "btn btn-secondary"}
              onClick={() =>
                setTitleModal({
                  open: true,
                  mode: "create",
                  title: `大纲 v${Math.max(1, outlines.length + 1)}`,
                })
              }
              type="button"
            >
              新建
            </button>

            <button
              className="btn btn-secondary"
              disabled={!activeOutlineId}
              onClick={() =>
                setTitleModal({
                  open: true,
                  mode: "rename",
                  title: activeOutline?.title ?? "",
                })
              }
              type="button"
            >
              重命名
            </button>

            <button
              className="btn btn-ghost text-danger hover:bg-danger/10"
              disabled={!activeOutlineId}
              onClick={() => void deleteOutline()}
              type="button"
            >
              删除
            </button>
          </div>
          <div className="text-xs text-subtext">
            {outlines.find((o) => o.id === activeOutlineId)?.has_chapters ? "该大纲已有章节" : "该大纲暂无章节"}
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <button
            className={canCreateChapters ? "btn btn-primary" : "btn btn-secondary"}
            disabled={!canCreateChapters}
            onClick={() => void createChaptersFromOutline()}
            title={canCreateChapters ? undefined : "请先生成包含章节结构的大纲"}
            type="button"
          >
            从大纲创建章节骨架
          </button>
          <button className="btn btn-secondary" onClick={() => setGenModalOpen(true)} type="button">
            AI 生成大纲
          </button>
        </div>
        <button
          className={dirty ? "btn btn-primary" : "btn btn-secondary"}
          disabled={!dirty || saving}
          onClick={() => void save()}
          type="button"
        >
          保存大纲
        </button>
      </div>

      <div className="panel p-6 sm:p-8">
        <div className="text-sm text-ink">流程说明</div>
        <div className="mt-1 text-xs text-subtext">
          推荐流程：AI 生成大纲 → 预览并应用（覆盖/另存） → 编辑完善 → 从大纲创建章节骨架 → 进入写作。
        </div>
        <div className="mt-1 text-[11px] text-subtext">
          提示：若 “从大纲创建章节骨架” 不可用，请先用 AI 生成大纲并应用（需要解析到章节结构）。
        </div>
      </div>

      <MarkdownEditor
        value={content}
        onChange={setContent}
        placeholder="在这里编写大纲（Markdown）..."
        minRows={16}
        name="outline_content_md"
      />

      <div className="text-xs text-subtext">快捷键：Ctrl/Cmd + S 保存</div>

      <Modal
        open={titleModal.open}
        onClose={() => setTitleModal((v) => ({ ...v, open: false }))}
        panelClassName="surface max-w-md p-6"
        ariaLabel={titleModal.mode === "create" ? "新建大纲" : "重命名大纲"}
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="font-content text-2xl">{titleModal.mode === "create" ? "新建大纲" : "重命名大纲"}</div>
            <div className="mt-1 text-xs text-subtext">用于在多个大纲之间切换工作流。</div>
          </div>
          <button
            className="btn btn-secondary"
            onClick={() => setTitleModal((v) => ({ ...v, open: false }))}
            type="button"
          >
            关闭
          </button>
        </div>

        <div className="mt-4 grid gap-3">
          <label className="grid gap-1">
            <span className="text-xs text-subtext">标题</span>
            <input
              className="input"
              name="outline_title"
              value={titleModal.title}
              onChange={(e) => setTitleModal((v) => ({ ...v, title: e.target.value }))}
            />
          </label>
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button
            className="btn btn-secondary"
            onClick={() => setTitleModal((v) => ({ ...v, open: false }))}
            type="button"
          >
            取消
          </button>
          <button
            className="btn btn-primary"
            onClick={async () => {
              const title = titleModal.title.trim();
              if (!title) {
                toast.toastError("标题不能为空");
                return;
              }
              if (titleModal.mode === "create") {
                if (dirty) {
                  const choice = await confirm.choose({
                    title: "当前大纲有未保存修改，是否继续？",
                    description: "保存后再切换可保留修改；不保存继续将丢失未保存内容。",
                    confirmText: "保存并继续",
                    secondaryText: "不保存继续",
                    cancelText: "取消",
                  });
                  if (choice === "cancel") return;
                  if (choice === "confirm") {
                    const ok = await save();
                    if (!ok) return;
                  }
                }
                setTitleModal((v) => ({ ...v, open: false }));
                await createOutline(title, "", null);
                return;
              }

              setTitleModal((v) => ({ ...v, open: false }));
              await renameOutline(title);
            }}
            type="button"
          >
            确认
          </button>
        </div>
      </Modal>

      <Modal
        open={genModalOpen}
        onClose={() => setGenModalOpen(false)}
        panelClassName="surface max-w-2xl p-6"
        ariaLabel="AI 生成大纲"
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="font-content text-2xl">AI 生成大纲</div>
            <div className="mt-1 text-xs text-subtext">生成结果会先预览，可选择覆盖当前大纲或另存为新大纲。</div>
          </div>
          <button className="btn btn-secondary" onClick={() => setGenModalOpen(false)} type="button">
            关闭
          </button>
        </div>

        <div className="mt-4 grid gap-4">
          <div className="rounded-atelier border border-border bg-canvas p-4">
            <div className="text-sm text-ink">基础参数</div>
            <div className="mt-1 text-xs text-subtext">先用章节数 / 基调 / 节奏定方向；生成后可在预览里再微调。</div>
            <div className="mt-3 grid gap-4 sm:grid-cols-3">
              <label className="grid gap-1">
                <span className="text-xs text-subtext">章节数</span>
                <input
                  className="input"
                  type="number"
                  min={1}
                  name="chapter_count"
                  value={genForm.chapter_count}
                  onChange={(e) => setGenForm((v) => ({ ...v, chapter_count: Number(e.target.value) }))}
                />
                <div className="text-[11px] text-subtext">可填写长篇目标（如 100/200）；系统会自动压缩每章粒度以尽量覆盖目标章节数。</div>
              </label>
              <label className="grid gap-1 sm:col-span-2">
                <span className="text-xs text-subtext">基调</span>
                <input
                  className="input"
                  name="tone"
                  value={genForm.tone}
                  onChange={(e) => setGenForm((v) => ({ ...v, tone: e.target.value }))}
                  placeholder="例如：现实主义，克制但有爆点"
                />
              </label>
              <label className="grid gap-1 sm:col-span-3">
                <span className="text-xs text-subtext">节奏</span>
                <input
                  className="input"
                  name="pacing"
                  value={genForm.pacing}
                  onChange={(e) => setGenForm((v) => ({ ...v, pacing: e.target.value }))}
                  placeholder="例如：前3章强钩子，中段升级，结尾反转"
                />
              </label>
            </div>
          </div>

          <div className="rounded-atelier border border-border bg-canvas p-4">
            <div className="text-sm text-ink">高级参数</div>
            <div className="mt-1 text-xs text-subtext">
              注入世界观/角色卡可让生成更贴近项目设定；流式生成会更快看到输出（偶发会自动回退非流式）。
            </div>
            <div className="mt-3 grid gap-3 sm:grid-cols-3">
              <label className="flex items-center gap-2 text-sm text-ink">
                <input
                  className="checkbox"
                  checked={genForm.include_world_setting}
                  name="include_world_setting"
                  onChange={(e) => setGenForm((v) => ({ ...v, include_world_setting: e.target.checked }))}
                  type="checkbox"
                />
                注入世界观
              </label>
              <label className="flex items-center gap-2 text-sm text-ink">
                <input
                  className="checkbox"
                  checked={genForm.include_characters}
                  name="include_characters"
                  onChange={(e) => setGenForm((v) => ({ ...v, include_characters: e.target.checked }))}
                  type="checkbox"
                />
                注入角色卡
              </label>
              <label className="flex items-center gap-2 text-sm text-ink sm:col-span-3">
                <input
                  className="checkbox"
                  checked={genStreamEnabled}
                  name="stream"
                  onChange={(e) => setGenStreamEnabled(e.target.checked)}
                  type="checkbox"
                />
                流式生成（beta）
              </label>
            </div>
          </div>
        </div>

        {genStreamEnabled ? (
          <div className="mt-4 grid gap-3">
            {genStreamProgress ? (
              <div className="panel p-3">
                <div className="flex items-center justify-between gap-2 text-xs text-subtext">
                  <span className="truncate">{genStreamProgress.message}</span>
                  <span className="shrink-0">{genStreamProgress.progress}%</span>
                </div>
                <div className="h-2 w-full rounded bg-border">
                  <div
                    className="h-2 rounded bg-accent motion-safe:transition-[width] motion-safe:duration-atelier motion-safe:ease-atelier"
                    style={{ width: `${Math.max(0, Math.min(100, genStreamProgress.progress))}%` }}
                  />
                </div>
              </div>
            ) : null}

            {genStreamText ? (
              <details className="panel p-3" open={generating}>
                <summary className="ui-transition-fast cursor-pointer text-xs text-subtext hover:text-ink">
                  流式输出预览（raw）
                </summary>
                <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words text-xs text-ink">
                  {genStreamText}
                </pre>
              </details>
            ) : null}
          </div>
        ) : null}

        <div className="mt-5 text-xs text-subtext">
          风险提示：生成会调用模型，可能消耗 token 与时间；请先预览再应用（推荐先另存为新大纲）。
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <button
            className="btn btn-secondary"
            onClick={() => {
              genStreamClientRef.current?.abort();
              setGenModalOpen(false);
            }}
            type="button"
          >
            取消
          </button>
          {generating && genStreamEnabled ? (
            <button
              className="btn btn-secondary"
              onClick={() => {
                genStreamClientRef.current?.abort();
              }}
              type="button"
            >
              取消生成
            </button>
          ) : null}
          <button
            className="btn btn-primary"
            disabled={generating}
            onClick={async () => {
              if (!projectId || !preset) return;
              setGenerating(true);
              genStreamClientRef.current = null;
              genStreamHasChunkRef.current = false;
              setGenStreamText("");
              setGenStreamProgress(null);
              try {
                const headers: Record<string, string> = { "X-LLM-Provider": preset.provider };
                const payload = {
                  requirements: {
                    chapter_count: genForm.chapter_count,
                    tone: genForm.tone,
                    pacing: genForm.pacing,
                  },
                  context: {
                    include_world_setting: genForm.include_world_setting,
                    include_characters: genForm.include_characters,
                  },
                };

                if (genStreamEnabled) {
                  setGenStreamProgress({ message: "开始生成...", progress: 0, status: "processing" });
                  const client = new SSEPostClient(`/api/projects/${projectId}/outline/generate-stream`, payload, {
                    headers,
                    onProgress: ({ message, progress, status }) => {
                      setGenStreamProgress({ message, progress, status });
                    },
                    onChunk: (content) => {
                      genStreamHasChunkRef.current = true;
                      setGenStreamText((prev) => prev + content);
                    },
                    onResult: (data) => {
                      setGenPreview(data as OutlineGenResult);
                    },
                  });
                  genStreamClientRef.current = client;

                  try {
                    await client.connect();
                    toast.toastSuccess("生成完成");
                  } catch (e) {
                    const err = e as unknown;
                    if (err instanceof SSEError && err.code !== "SSE_SERVER_ERROR" && err.code !== "ABORTED") {
                      if (!genStreamHasChunkRef.current) {
                        toast.toastError("流式生成失败，已回退非流式");
                        const res = await apiJson<OutlineGenResult>(`/api/projects/${projectId}/outline/generate`, {
                          method: "POST",
                          headers,
                          body: JSON.stringify(payload),
                        });
                        setGenPreview(res.data);
                        toast.toastSuccess("生成完成");
                      } else {
                        toast.toastError(`${err.message} (${err.code})`, err.requestId);
                      }
                      return;
                    }
                    if (err instanceof SSEError && err.code === "SSE_SERVER_ERROR") {
                      toast.toastError(`${err.message} (${err.code})`, err.requestId);
                      return;
                    }
                    if (err instanceof SSEError && err.code === "ABORTED") {
                      toast.toastSuccess("已取消生成");
                      return;
                    }
                    if (err instanceof ApiError) {
                      toast.toastError(`${err.message} (${err.code})`, err.requestId);
                      return;
                    }
                    toast.toastError("流式生成失败");
                  }
                } else {
                  const res = await apiJson<OutlineGenResult>(`/api/projects/${projectId}/outline/generate`, {
                    method: "POST",
                    headers,
                    body: JSON.stringify(payload),
                  });
                  setGenPreview(res.data);
                  toast.toastSuccess("生成完成");
                }
              } catch (e) {
                const err = e as ApiError;
                toast.toastError(`${err.message} (${err.code})`, err.requestId);
              } finally {
                setGenerating(false);
              }
            }}
            type="button"
          >
            {generating ? "生成中..." : "生成"}
          </button>
        </div>

        {genPreview ? (
          <div className="panel mt-6 p-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-sm text-ink">生成结果预览</div>
                <div className="mt-1 text-xs text-subtext">
                  解析章节：{genPreview.chapters.length}{" "}
                  {genPreview.parse_error ? `（${genPreview.parse_error.message}）` : ""}
                </div>
                <div className="mt-1 text-[11px] text-subtext">
                  应用方式：覆盖会替换当前大纲并立即保存；另存会创建新大纲并切换（更安全，推荐）。
                </div>
              </div>
              <div className="flex gap-2">
                <button className="btn btn-secondary" onClick={() => setGenPreview(null)} type="button">
                  取消
                </button>
                <button
                  className="btn btn-secondary"
                  onClick={async () => {
                    const ok = !dirty
                      ? true
                      : await confirm.confirm({
                          title: "覆盖当前未保存的大纲？",
                          description: "覆盖后将以生成结果替换当前大纲，并立即保存。",
                          confirmText: "覆盖并保存",
                          danger: true,
                        });
                    if (!ok) return;
                    setGenModalOpen(false);
                    await save(genPreview.outline_md, { chapters: genPreview.chapters });
                    setGenPreview(null);
                  }}
                  type="button"
                >
                  覆盖当前大纲并保存
                </button>
                <button className="btn btn-primary" onClick={() => void saveGeneratedAsNewOutline()} type="button">
                  保存为新大纲并切换
                </button>
              </div>
            </div>
            <div className="mt-3">
              <MarkdownEditor
                value={genPreview.outline_md}
                onChange={(next) => setGenPreview((prev) => (prev ? { ...prev, outline_md: next } : prev))}
                minRows={10}
                name="generated_outline_preview"
              />
            </div>
          </div>
        ) : null}
      </Modal>

      <WizardNextBar
        projectId={projectId}
        currentStep="outline"
        progress={wizard.progress}
        loading={wizard.loading}
        dirty={dirty}
        saving={saving || generating}
        onSave={() => save()}
        primaryAction={
          wizard.progress.nextStep?.key === "chapters"
            ? canCreateChapters
              ? { label: "下一步：创建章节骨架", disabled: generating || saving, onClick: createChaptersFromOutline }
              : {
                  label: "下一步：先 AI 生成大纲",
                  disabled: generating || saving,
                  onClick: () => setGenModalOpen(true),
                }
            : undefined
        }
      />
    </div>
  );
}
