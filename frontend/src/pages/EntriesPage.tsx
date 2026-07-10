import { motion, useReducedMotion } from "framer-motion";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { WizardNextBar } from "../components/atelier/WizardNextBar";
import { Drawer } from "../components/ui/Drawer";
import { useConfirm } from "../components/ui/confirm";
import { useToast } from "../components/ui/toast";
import { useAutoSave } from "../hooks/useAutoSave";
import { useIsMobile } from "../hooks/useIsMobile";
import { useProjectData } from "../hooks/useProjectData";
import { useQueuedSave } from "../hooks/useQueuedSave";
import { useWizardProgress } from "../hooks/useWizardProgress";
import { copyText } from "../lib/copyText";
import { duration, transition } from "../lib/motion";
import { ApiError } from "../services/apiClient";
import { createEntry, deleteEntry, listEntries, updateEntry } from "../services/entriesApi";
import { markWizardProjectChanged } from "../services/wizard";
import type { Entry } from "../types";

type EntryForm = {
  title: string;
  content: string;
  tags: string[];
};

type EntrySaveRequest = {
  silent: boolean;
  close: boolean;
  snapshot: EntryForm;
  sessionId: number;
  targetId: string | null;
};

const ALL_TAG_LABEL = "全部";
const PAGE_SIZE = 200;

function normalizeEntryTags(tags: string[]): string[] {
  const seen = new Set<string>();
  const next: string[] = [];
  for (const raw of tags) {
    const tag = String(raw ?? "").trim();
    if (!tag) continue;
    const key = tag.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    next.push(tag);
    if (next.length >= 80) break;
  }
  return next;
}

function sameTags(left: string[], right: string[]): boolean {
  if (left.length !== right.length) return false;
  return left.every((value, index) => value === right[index]);
}

function sameEntryForm(left: EntryForm, right: EntryForm): boolean {
  return left.title === right.title && left.content === right.content && sameTags(left.tags, right.tags);
}

function toEntryForm(entry: Entry | null): EntryForm {
  if (!entry) return { title: "", content: "", tags: [] };
  return {
    title: entry.title ?? "",
    content: entry.content ?? "",
    tags: normalizeEntryTags(entry.tags ?? []),
  };
}

function tagBadgeClass(tag: string): string {
  switch (tag) {
    case "设定":
      return "bg-blue-500/15 text-blue-700";
    case "伏笔":
      return "bg-amber-500/15 text-amber-700";
    case "情节":
      return "bg-green-500/15 text-green-700";
    default:
      return "bg-slate-500/15 text-subtext";
  }
}

function filterChipClass(active: boolean): string {
  return active ? "border-accent/40 bg-accent/10 text-ink" : "border-border bg-canvas text-subtext hover:bg-surface";
}

function formatApiError(error: unknown): { message: string; requestId?: string } {
  if (error instanceof ApiError) {
    return { message: `${error.message} (${error.code})`, requestId: error.requestId };
  }
  return { message: "请求失败 (UNKNOWN_ERROR)" };
}

export function EntriesPage() {
  const DEFAULT_ENTRY_TAGS = ["设定", "伏笔", "情节"] as const;

  const { projectId } = useParams();
  const toast = useToast();
  const confirm = useConfirm();
  const reduceMotion = useReducedMotion();
  const wizard = useWizardProgress(projectId);
  const isMobile = useIsMobile();
  const refreshWizard = wizard.refresh;
  const bumpWizardLocal = wizard.bumpLocal;

  const [loadError, setLoadError] = useState<null | { message: string; code: string; requestId?: string }>(null);

  const entriesQuery = useProjectData<Entry[]>(projectId, async (id) => {
    try {
      const items: Entry[] = [];
      let offset = 0;

      while (true) {
        const page = await listEntries(id, { limit: PAGE_SIZE, offset });
        items.push(...page.items);
        if (typeof page.next_offset !== "number") break;
        offset = page.next_offset;
      }

      setLoadError(null);
      return items;
    } catch (error) {
      if (error instanceof ApiError) {
        setLoadError({ message: error.message, code: error.code, requestId: error.requestId });
      } else {
        setLoadError({ message: "请求失败", code: "UNKNOWN_ERROR" });
      }
      throw error;
    }
  });
  const entries = useMemo(() => entriesQuery.data ?? [], [entriesQuery.data]);
  const loading = entriesQuery.loading;

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState<Entry | null>(null);
  const nextEditorSessionIdRef = useRef(0);
  const editorSessionRef = useRef({ id: 0, targetId: null as string | null });
  const createdTargetRef = useRef<{ sessionId: number; targetId: string } | null>(null);
  const wizardRefreshTimerRef = useRef<number | null>(null);
  const [baseline, setBaseline] = useState<EntryForm | null>(null);
  const [form, setForm] = useState<EntryForm>({ title: "", content: "", tags: [] });
  const [searchText, setSearchText] = useState("");
  const [activeTag, setActiveTag] = useState<string>(ALL_TAG_LABEL);
  const [customTagInput, setCustomTagInput] = useState("");

  const load = entriesQuery.refresh;
  const setEntries = entriesQuery.setData;

  useEffect(() => {
    return () => {
      if (wizardRefreshTimerRef.current !== null) {
        window.clearTimeout(wizardRefreshTimerRef.current);
      }
    };
  }, []);

  const filteredEntries = useMemo(() => {
    const query = searchText.trim().toLowerCase();
    return entries.filter((entry) => {
      if (activeTag !== ALL_TAG_LABEL && !entry.tags.includes(activeTag)) return false;
      if (!query) return true;

      const title = String(entry.title ?? "").toLowerCase();
      const content = String(entry.content ?? "").toLowerCase();
      const tags = entry.tags.map((tag) => String(tag ?? "").toLowerCase());
      return title.includes(query) || content.includes(query) || tags.some((tag) => tag.includes(query));
    });
  }, [activeTag, entries, searchText]);

  const dirty = useMemo(() => {
    if (!baseline) return false;
    return !sameEntryForm(form, baseline);
  }, [baseline, form]);

  const hasFilters = useMemo(() => Boolean(searchText.trim()) || activeTag !== ALL_TAG_LABEL, [activeTag, searchText]);

  const openNew = useCallback(() => {
    const sessionId = nextEditorSessionIdRef.current + 1;
    nextEditorSessionIdRef.current = sessionId;
    editorSessionRef.current = { id: sessionId, targetId: null };
    setEditing(null);
    const next = toEntryForm(null);
    setForm(next);
    setBaseline(next);
    setCustomTagInput("");
    setDrawerOpen(true);
  }, []);

  const openEdit = useCallback((entry: Entry) => {
    const sessionId = nextEditorSessionIdRef.current + 1;
    nextEditorSessionIdRef.current = sessionId;
    editorSessionRef.current = { id: sessionId, targetId: entry.id };
    setEditing(entry);
    const next = toEntryForm(entry);
    setForm(next);
    setBaseline(next);
    setCustomTagInput("");
    setDrawerOpen(true);
  }, []);

  const closeDrawer = useCallback(async () => {
    if (dirty) {
      const ok = await confirm.confirm({
        title: "放弃未保存修改？",
        description: "关闭后未保存内容会丢失。你可以先点击“保存”再关闭。",
        confirmText: "放弃",
        cancelText: "取消",
        danger: true,
      });
      if (!ok) return;
    }
    setDrawerOpen(false);
  }, [confirm, dirty]);

  const addTag = useCallback((rawTag: string) => {
    const tag = rawTag.trim();
    if (!tag) return;
    setForm((current) => {
      const nextTags = normalizeEntryTags([...current.tags, tag]);
      return sameTags(current.tags, nextTags) ? current : { ...current, tags: nextTags };
    });
  }, []);

  const removeTag = useCallback((tag: string) => {
    setForm((current) => ({ ...current, tags: current.tags.filter((item) => item !== tag) }));
  }, []);

  const addCustomTag = useCallback(() => {
    const next = customTagInput.trim();
    if (!next) return;
    addTag(next);
    setCustomTagInput("");
  }, [addTag, customTagInput]);

  const performSaveEntry = useCallback(
    async (request: EntrySaveRequest) => {
      if (!projectId) return false;
      const { close, sessionId, silent, snapshot } = request;
      const title = snapshot.title.trim();
      if (!title) return false;

      const scheduleWizardRefresh = () => {
        if (wizardRefreshTimerRef.current !== null) {
          window.clearTimeout(wizardRefreshTimerRef.current);
        }
        wizardRefreshTimerRef.current = window.setTimeout(() => void refreshWizard(), 1200);
      };

      try {
        const payload = {
          title,
          content: snapshot.content,
          tags: normalizeEntryTags(snapshot.tags),
        };

        const createdTarget = createdTargetRef.current;
        const targetId = request.targetId ?? (createdTarget?.sessionId === sessionId ? createdTarget.targetId : null);
        const saved = !targetId ? await createEntry(projectId, payload) : await updateEntry(targetId, payload);

        if (!targetId) createdTargetRef.current = { sessionId, targetId: saved.id };
        setEntries((previous) => {
          const list = previous ?? [];
          return [saved, ...list.filter((entry) => entry.id !== saved.id)];
        });

        const nextBaseline = toEntryForm(saved);
        if (editorSessionRef.current.id === sessionId) {
          editorSessionRef.current = { id: sessionId, targetId: saved.id };
          setEditing(saved);
          setBaseline(nextBaseline);
          setForm((current) => (sameEntryForm(current, snapshot) ? nextBaseline : current));
        }

        markWizardProjectChanged(projectId);
        bumpWizardLocal();
        if (silent) scheduleWizardRefresh();
        else await refreshWizard();
        if (!silent) toast.toastSuccess("已保存");
        if (close && editorSessionRef.current.id === sessionId) setDrawerOpen(false);
        return true;
      } catch (error) {
        const apiError = formatApiError(error);
        toast.toastError(apiError.message, apiError.requestId);
        return false;
      }
    },
    [bumpWizardLocal, projectId, refreshWizard, setEntries, toast],
  );

  const { save: queueEntrySave, saving } = useQueuedSave(performSaveEntry);
  const saveEntry = useCallback(
    (opts?: { silent?: boolean; close?: boolean; snapshot?: EntryForm }) => {
      const snapshot = opts?.snapshot ?? form;
      return queueEntrySave({
        silent: Boolean(opts?.silent),
        close: Boolean(opts?.close),
        snapshot: { ...snapshot, tags: [...snapshot.tags] },
        sessionId: editorSessionRef.current.id,
        targetId: editorSessionRef.current.targetId,
      });
    },
    [form, queueEntrySave],
  );

  const handleDelete = useCallback(
    async (entry: Entry) => {
      const ok = await confirm.confirm({
        title: "删除条目？",
        description: "该条目将从项目中移除。",
        confirmText: "删除",
        cancelText: "取消",
        danger: true,
      });
      if (!ok) return;

      try {
        await deleteEntry(entry.id);
        setEntries((previous) => (previous ?? []).filter((item) => item.id !== entry.id));

        if (editing?.id === entry.id) {
          const sessionId = nextEditorSessionIdRef.current + 1;
          nextEditorSessionIdRef.current = sessionId;
          editorSessionRef.current = { id: sessionId, targetId: null };
          setDrawerOpen(false);
          setEditing(null);
          setBaseline(null);
          setForm(toEntryForm(null));
          setCustomTagInput("");
        }

        if (projectId) markWizardProjectChanged(projectId);
        bumpWizardLocal();
        toast.toastSuccess("已删除");
        await refreshWizard();
      } catch (error) {
        const apiError = formatApiError(error);
        toast.toastError(apiError.message, apiError.requestId);
      }
    },
    [bumpWizardLocal, confirm, editing?.id, projectId, refreshWizard, setEntries, toast],
  );

  useAutoSave({
    enabled: drawerOpen && Boolean(projectId) && Boolean(baseline),
    dirty,
    saveOnIdle: true,
    delayMs: 900,
    getSnapshot: () => ({ ...form, tags: [...form.tags] }),
    onSave: async (snapshot) => {
      await saveEntry({ silent: true, close: false, snapshot });
    },
    deps: [editing?.id ?? "", form.title, form.content, form.tags.join("\u0001")],
  });

  return (
    <div className="grid gap-4 pb-[calc(6rem+env(safe-area-inset-bottom))]">
      <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
        <div className="flex flex-1 flex-col gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <div className="text-sm text-subtext">
              {hasFilters ? `共 ${filteredEntries.length}/${entries.length} 个条目` : `共 ${entries.length} 个条目`}
            </div>
            <input
              className="input-underline w-full sm:w-72"
              value={searchText}
              onChange={(event) => setSearchText(event.target.value)}
              placeholder="搜索：标题 / 内容 / 标签"
              aria-label="条目搜索"
            />
            {searchText.trim() ? (
              <button className="btn btn-ghost px-3 py-2 text-xs" onClick={() => setSearchText("")} type="button">
                清空搜索
              </button>
            ) : null}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {[ALL_TAG_LABEL, ...DEFAULT_ENTRY_TAGS].map((tag) => (
              <button
                key={tag}
                className={`ui-focus-ring rounded-full border px-3 py-1 text-xs ${filterChipClass(activeTag === tag)}`}
                onClick={() => setActiveTag(tag)}
                type="button"
              >
                {tag}
              </button>
            ))}
          </div>
        </div>

        <button className="btn btn-primary" onClick={openNew} type="button">
          新增条目
        </button>
      </div>

      {loading && entriesQuery.data === null ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          {Array.from({ length: 4 }).map((_, idx) => (
            <div key={idx} className="panel p-6">
              <div className="skeleton h-5 w-24" />
              <div className="mt-3 flex gap-2">
                <div className="skeleton h-5 w-12 rounded-full" />
                <div className="skeleton h-5 w-12 rounded-full" />
              </div>
              <div className="mt-3 grid gap-2">
                <div className="skeleton h-4 w-full" />
                <div className="skeleton h-4 w-5/6" />
                <div className="skeleton h-4 w-2/3" />
              </div>
            </div>
          ))}
        </div>
      ) : null}

      {!loading && entriesQuery.data === null && loadError ? (
        <div className="error-card">
          <div className="state-title">加载失败</div>
          <div className="state-desc">{`${loadError.message} (${loadError.code})`}</div>
          {loadError.requestId ? (
            <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-subtext">
              <span>request_id: {loadError.requestId}</span>
              <button
                className="btn btn-secondary btn-sm"
                onClick={() => void copyText(loadError.requestId!, { title: "复制 request_id" })}
                type="button"
              >
                复制 request_id
              </button>
            </div>
          ) : null}
          <div className="mt-4 flex flex-wrap gap-2">
            <button className="btn btn-primary" onClick={() => void load()} type="button">
              重试
            </button>
          </div>
        </div>
      ) : null}

      {!loading && !loadError && entries.length === 0 ? (
        <div className="panel p-6">
          <div className="font-content text-xl text-ink">暂无条目</div>
          <div className="mt-2 text-sm text-subtext">
            建议先记录世界设定、伏笔线索或关键情节，后续写作时会更容易统一细节与回收线索。
          </div>
          <button className="btn btn-primary mt-4" onClick={openNew} type="button">
            新增条目
          </button>
        </div>
      ) : null}

      {!loading && !loadError && entries.length > 0 && filteredEntries.length === 0 ? (
        <div className="panel p-6">
          <div className="font-content text-xl text-ink">没有匹配的条目</div>
          <div className="mt-2 text-sm text-subtext">尝试修改搜索关键词，或切换标签筛选后再查看全部条目。</div>
          <div className="mt-4 flex flex-wrap gap-2">
            <button className="btn btn-secondary" onClick={() => setSearchText("")} type="button">
              清空搜索
            </button>
            {activeTag !== ALL_TAG_LABEL ? (
              <button className="btn btn-secondary" onClick={() => setActiveTag(ALL_TAG_LABEL)} type="button">
                查看全部标签
              </button>
            ) : null}
          </div>
        </div>
      ) : null}

      <motion.div
        className="grid grid-cols-1 gap-4 sm:grid-cols-2"
        initial="hidden"
        animate="show"
        variants={{
          hidden: {},
          show: { transition: { staggerChildren: reduceMotion ? 0 : duration.stagger } },
        }}
      >
        {filteredEntries.map((entry) => (
          <motion.div
            key={entry.id}
            className="panel-interactive ui-focus-ring p-6 text-left"
            initial="hidden"
            animate="show"
            variants={{
              hidden: reduceMotion ? { opacity: 0 } : { opacity: 0, y: 8 },
              show: reduceMotion ? { opacity: 1 } : { opacity: 1, y: 0 },
            }}
            transition={reduceMotion ? transition.reduced : transition.slow}
            whileHover={reduceMotion ? undefined : { y: -2, transition: transition.fast }}
            whileTap={reduceMotion ? undefined : { y: 0, scale: 0.98, transition: transition.fast }}
            onClick={() => openEdit(entry)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                openEdit(entry);
              }
            }}
            role="button"
            tabIndex={0}
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="truncate font-content text-xl text-ink">{entry.title}</div>
                {entry.tags.length > 0 ? (
                  <div className="mt-3 flex flex-wrap gap-2">
                    {entry.tags.map((tag) => (
                      <span
                        key={`${entry.id}-${tag}`}
                        className={`rounded-full px-2 py-0.5 text-[11px] ${tagBadgeClass(tag)}`}
                      >
                        {tag}
                      </span>
                    ))}
                  </div>
                ) : null}
              </div>
              <button
                className="btn btn-ghost px-3 py-2 text-xs text-danger hover:bg-danger/10"
                onClick={async (event) => {
                  event.stopPropagation();
                  await handleDelete(entry);
                }}
                type="button"
              >
                删除
              </button>
            </div>

            <div className="mt-3 line-clamp-4 text-sm text-subtext">
              {entry.content?.trim() ? entry.content : "暂无内容，点击继续补充设定、伏笔或情节摘要。"}
            </div>
          </motion.div>
        ))}
      </motion.div>

      <Drawer
        open={drawerOpen}
        onClose={() => void closeDrawer()}
        side={isMobile ? "bottom" : "right"}
        panelClassName="h-[85dvh] sm:h-full w-full sm:max-w-xl border-t sm:border-t-0 sm:border-l border-border bg-canvas p-4 sm:p-6 shadow-sm"
        ariaLabel={editing ? "编辑条目" : "新增条目"}
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="font-content text-2xl text-ink">{editing ? "编辑条目" : "新增条目"}</div>
            <div className="mt-1 text-xs text-subtext">{dirty ? "未保存" : "已保存"}</div>
          </div>
          <div className="flex gap-2">
            <button className="btn btn-secondary" onClick={() => void closeDrawer()} type="button">
              关闭
            </button>
            <button
              className="btn btn-primary"
              disabled={saving || !form.title.trim()}
              onClick={() => void saveEntry({ silent: false, close: true })}
              type="button"
            >
              保存
            </button>
          </div>
        </div>

        <div className="mt-5 grid gap-4">
          <label className="grid gap-1">
            <span className="text-xs text-subtext">标题</span>
            <input
              className="input"
              name="title"
              value={form.title}
              onChange={(event) => setForm((current) => ({ ...current, title: event.target.value }))}
              placeholder="例如：雨夜相遇 / 皇城禁令 / 第三章伏笔"
            />
            <div className="text-[11px] text-subtext">标题建议具体可检索，方便后续快速定位与复用。</div>
          </label>

          <div className="grid gap-3">
            <div className="grid gap-1">
              <span className="text-xs text-subtext">标签</span>
              {form.tags.length > 0 ? (
                <div className="flex flex-wrap gap-2">
                  {form.tags.map((tag) => (
                    <button
                      key={`selected-${tag}`}
                      className={`inline-flex items-center gap-1 rounded-full px-2 py-1 text-[11px] ${tagBadgeClass(tag)}`}
                      onClick={() => removeTag(tag)}
                      type="button"
                    >
                      <span>{tag}</span>
                      <span aria-hidden="true">×</span>
                    </button>
                  ))}
                </div>
              ) : (
                <div className="text-[11px] text-subtext">还没有标签，可先选择默认标签或添加自定义标签。</div>
              )}
            </div>

            <div className="flex flex-wrap gap-2">
              {DEFAULT_ENTRY_TAGS.map((tag) => {
                const selected = form.tags.includes(tag);
                return (
                  <button
                    key={`default-${tag}`}
                    className={`ui-focus-ring rounded-full border px-3 py-1 text-xs ${filterChipClass(selected)}`}
                    onClick={() => addTag(tag)}
                    type="button"
                  >
                    {selected ? `已选：${tag}` : `添加：${tag}`}
                  </button>
                );
              })}
            </div>

            <div className="flex flex-col gap-2 sm:flex-row">
              <input
                className="input"
                value={customTagInput}
                onChange={(event) => setCustomTagInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key !== "Enter") return;
                  event.preventDefault();
                  addCustomTag();
                }}
                placeholder="输入自定义标签"
              />
              <button className="btn btn-secondary sm:shrink-0" onClick={addCustomTag} type="button">
                添加标签
              </button>
            </div>
            <div className="text-[11px] text-subtext">再次点击已选标签即可移除；自定义标签会以灰色 badge 展示。</div>
          </div>

          <label className="grid gap-1">
            <span className="text-xs text-subtext">内容</span>
            <textarea
              className="textarea atelier-content h-52 resize-y sm:h-auto"
              name="content"
              rows={12}
              value={form.content}
              onChange={(event) => setForm((current) => ({ ...current, content: event.target.value }))}
              placeholder="记录设定细节、伏笔安排、情节想法、信息来源或后续待验证点…"
            />
            <div className="text-[11px] text-subtext">建议一条只写一个主题，后续搜索和回收会更清晰。</div>
          </label>
        </div>
      </Drawer>

      <WizardNextBar
        projectId={projectId}
        currentStep="characters"
        progress={wizard.progress}
        loading={wizard.loading}
        primaryAction={
          wizard.progress.nextStep?.key === "characters" ? { label: "本页：新增条目", onClick: openNew } : undefined
        }
      />
    </div>
  );
}
