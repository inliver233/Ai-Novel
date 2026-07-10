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
import { ApiError, apiJson } from "../services/apiClient";
import { markWizardProjectChanged } from "../services/wizard";
import type { Character } from "../types";

type CharacterForm = {
  name: string;
  role: string;
  profile: string;
  notes: string;
};

type CharacterSaveRequest = {
  silent: boolean;
  close: boolean;
  snapshot: CharacterForm;
  sessionId: number;
  targetId: string | null;
};

export function CharactersPage() {
  const { projectId } = useParams();
  const toast = useToast();
  const confirm = useConfirm();
  const reduceMotion = useReducedMotion();
  const wizard = useWizardProgress(projectId);
  const isMobile = useIsMobile();
  const refreshWizard = wizard.refresh;
  const bumpWizardLocal = wizard.bumpLocal;

  const [loadError, setLoadError] = useState<null | { message: string; code: string; requestId?: string }>(null);

  const charactersQuery = useProjectData<Character[]>(projectId, async (id) => {
    try {
      const res = await apiJson<{ characters: Character[] }>(`/api/projects/${id}/characters`);
      setLoadError(null);
      return res.data.characters;
    } catch (e) {
      if (e instanceof ApiError) {
        setLoadError({ message: e.message, code: e.code, requestId: e.requestId });
      } else {
        setLoadError({ message: "请求失败", code: "UNKNOWN_ERROR" });
      }
      throw e;
    }
  });
  const characters = useMemo(() => charactersQuery.data ?? [], [charactersQuery.data]);
  const loading = charactersQuery.loading;

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState<Character | null>(null);
  const nextEditorSessionIdRef = useRef(0);
  const editorSessionRef = useRef({ id: 0, targetId: null as string | null });
  const createdTargetRef = useRef<{ sessionId: number; targetId: string } | null>(null);
  const wizardRefreshTimerRef = useRef<number | null>(null);
  const [baseline, setBaseline] = useState<CharacterForm | null>(null);
  const [form, setForm] = useState<CharacterForm>({ name: "", role: "", profile: "", notes: "" });
  const [searchText, setSearchText] = useState("");

  const filteredCharacters = useMemo(() => {
    const q = searchText.trim().toLowerCase();
    if (!q) return characters;
    return characters.filter((c) => {
      const name = String(c.name ?? "").toLowerCase();
      const role = String(c.role ?? "").toLowerCase();
      return name.includes(q) || role.includes(q);
    });
  }, [characters, searchText]);

  const dirty = useMemo(() => {
    if (!baseline) return false;
    return (
      form.name !== baseline.name ||
      form.role !== baseline.role ||
      form.profile !== baseline.profile ||
      form.notes !== baseline.notes
    );
  }, [baseline, form]);

  const load = charactersQuery.refresh;
  const setCharacters = charactersQuery.setData;

  useEffect(() => {
    return () => {
      if (wizardRefreshTimerRef.current !== null) window.clearTimeout(wizardRefreshTimerRef.current);
    };
  }, []);

  const openNew = () => {
    const sessionId = nextEditorSessionIdRef.current + 1;
    nextEditorSessionIdRef.current = sessionId;
    editorSessionRef.current = { id: sessionId, targetId: null };
    setEditing(null);
    const next = { name: "", role: "", profile: "", notes: "" };
    setForm(next);
    setBaseline(next);
    setDrawerOpen(true);
  };

  const openEdit = (c: Character) => {
    const sessionId = nextEditorSessionIdRef.current + 1;
    nextEditorSessionIdRef.current = sessionId;
    editorSessionRef.current = { id: sessionId, targetId: c.id };
    setEditing(c);
    const next = {
      name: c.name ?? "",
      role: c.role ?? "",
      profile: c.profile ?? "",
      notes: c.notes ?? "",
    };
    setForm(next);
    setBaseline(next);
    setDrawerOpen(true);
  };

  const closeDrawer = async () => {
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
  };

  const performSaveCharacter = useCallback(
    async (request: CharacterSaveRequest) => {
      if (!projectId) return false;
      const { close, sessionId, silent, snapshot } = request;
      if (!snapshot.name.trim()) return false;

      const scheduleWizardRefresh = () => {
        if (wizardRefreshTimerRef.current !== null) window.clearTimeout(wizardRefreshTimerRef.current);
        wizardRefreshTimerRef.current = window.setTimeout(() => void refreshWizard(), 1200);
      };

      try {
        const createdTarget = createdTargetRef.current;
        const targetId = request.targetId ?? (createdTarget?.sessionId === sessionId ? createdTarget.targetId : null);
        const res = !targetId
          ? await apiJson<{ character: Character }>(`/api/projects/${projectId}/characters`, {
              method: "POST",
              body: JSON.stringify({
                name: snapshot.name.trim(),
                role: snapshot.role.trim() || null,
                profile: snapshot.profile || null,
                notes: snapshot.notes || null,
              }),
            })
          : await apiJson<{ character: Character }>(`/api/characters/${targetId}`, {
              method: "PUT",
              body: JSON.stringify({
                name: snapshot.name.trim(),
                role: snapshot.role.trim() || null,
                profile: snapshot.profile || null,
                notes: snapshot.notes || null,
              }),
            });

        const saved = res.data.character;
        if (!targetId) createdTargetRef.current = { sessionId, targetId: saved.id };
        setCharacters((prev) => {
          const list = prev ?? [];
          const idx = list.findIndex((c) => c.id === saved.id);
          if (idx >= 0) return list.map((c) => (c.id === saved.id ? saved : c));
          return [saved, ...list];
        });

        const nextBaseline: CharacterForm = {
          name: saved.name ?? "",
          role: saved.role ?? "",
          profile: saved.profile ?? "",
          notes: saved.notes ?? "",
        };
        if (editorSessionRef.current.id === sessionId) {
          editorSessionRef.current = { id: sessionId, targetId: saved.id };
          setEditing(saved);
          setBaseline(nextBaseline);
          setForm((prev) => {
            if (
              prev.name === snapshot.name &&
              prev.role === snapshot.role &&
              prev.profile === snapshot.profile &&
              prev.notes === snapshot.notes
            ) {
              return nextBaseline;
            }
            return prev;
          });
        }

        markWizardProjectChanged(projectId);
        bumpWizardLocal();
        if (silent) scheduleWizardRefresh();
        else await refreshWizard();
        if (!silent) toast.toastSuccess("已保存");
        if (close && editorSessionRef.current.id === sessionId) setDrawerOpen(false);
        return true;
      } catch (err) {
        const apiErr = err as ApiError;
        toast.toastError(`${apiErr.message} (${apiErr.code})`, apiErr.requestId);
        return false;
      }
    },
    [bumpWizardLocal, projectId, refreshWizard, setCharacters, toast],
  );

  const { save: queueCharacterSave, saving } = useQueuedSave(performSaveCharacter);
  const saveCharacter = useCallback(
    (opts?: { silent?: boolean; close?: boolean; snapshot?: CharacterForm }) =>
      queueCharacterSave({
        silent: Boolean(opts?.silent),
        close: Boolean(opts?.close),
        snapshot: { ...(opts?.snapshot ?? form) },
        sessionId: editorSessionRef.current.id,
        targetId: editorSessionRef.current.targetId,
      }),
    [form, queueCharacterSave],
  );

  useAutoSave({
    enabled: drawerOpen && Boolean(projectId) && Boolean(baseline),
    dirty,
    delayMs: 900,
    getSnapshot: () => ({ ...form }),
    onSave: async (snapshot) => {
      await saveCharacter({ silent: true, close: false, snapshot });
    },
    deps: [editing?.id ?? "", form.name, form.role, form.profile, form.notes],
  });

  return (
    <div className="grid gap-4 pb-[calc(6rem+env(safe-area-inset-bottom))]">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-wrap items-center gap-3">
          <div className="text-sm text-subtext">
            {searchText.trim()
              ? `共 ${filteredCharacters.length}/${characters.length} 位角色`
              : `共 ${characters.length} 位角色`}
          </div>
          <input
            className="input-underline w-full sm:w-64"
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
            placeholder="搜索：姓名 / 定位"
            aria-label="角色搜索"
          />
          {searchText.trim() ? (
            <button className="btn btn-ghost px-3 py-2 text-xs" onClick={() => setSearchText("")} type="button">
              清空搜索
            </button>
          ) : null}
        </div>
        <button className="btn btn-primary" onClick={openNew} type="button">
          新增角色
        </button>
      </div>

      {loading && charactersQuery.data === null ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          {Array.from({ length: 4 }).map((_, idx) => (
            <div key={idx} className="panel p-6">
              <div className="skeleton h-5 w-24" />
              <div className="mt-3 grid gap-2">
                <div className="skeleton h-4 w-full" />
                <div className="skeleton h-4 w-5/6" />
                <div className="skeleton h-4 w-2/3" />
              </div>
            </div>
          ))}
        </div>
      ) : null}

      {!loading && charactersQuery.data === null && loadError ? (
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

      {!loading && !loadError && characters.length === 0 ? (
        <div className="panel p-6">
          <div className="font-content text-xl text-ink">暂无角色</div>
          <div className="mt-2 text-sm text-subtext">
            建议先创建 3-5 个关键角色（主角 / 反派 / 关键 NPC），再进入「大纲」生成章节。
          </div>
          <button className="btn btn-primary mt-4" onClick={openNew} type="button">
            新增角色
          </button>
        </div>
      ) : null}

      {!loading && !loadError && characters.length > 0 && filteredCharacters.length === 0 ? (
        <div className="panel p-6">
          <div className="font-content text-xl text-ink">没有匹配的角色</div>
          <div className="mt-2 text-sm text-subtext">尝试修改搜索关键词，或清空搜索后再查看全部角色。</div>
          <button className="btn btn-secondary mt-4" onClick={() => setSearchText("")} type="button">
            清空搜索
          </button>
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
        {filteredCharacters.map((c) => (
          <motion.div
            key={c.id}
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
            onClick={() => openEdit(c)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                openEdit(c);
              }
            }}
            role="button"
            tabIndex={0}
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="truncate font-content text-xl text-ink">{c.name}</div>
                <div className="mt-1 text-xs text-subtext">{c.role ?? "未填写角色定位"}</div>
              </div>
              <button
                className="btn btn-ghost px-3 py-2 text-xs text-danger hover:bg-danger/10"
                onClick={async (e) => {
                  e.stopPropagation();
                  const ok = await confirm.confirm({
                    title: "删除角色？",
                    description: "该角色将从项目中移除。",
                    confirmText: "删除",
                    danger: true,
                  });
                  if (!ok) return;
                  try {
                    await apiJson<Record<string, never>>(`/api/characters/${c.id}`, { method: "DELETE" });
                    if (projectId) markWizardProjectChanged(projectId);
                    bumpWizardLocal();
                    toast.toastSuccess("已删除");
                    await load();
                    await refreshWizard();
                  } catch (err) {
                    const apiErr = err as ApiError;
                    toast.toastError(`${apiErr.message} (${apiErr.code})`, apiErr.requestId);
                  }
                }}
                type="button"
              >
                删除
              </button>
            </div>
            {c.profile ? <div className="mt-3 line-clamp-4 text-sm text-subtext">{c.profile}</div> : null}
          </motion.div>
        ))}
      </motion.div>

      <Drawer
        open={drawerOpen}
        onClose={() => void closeDrawer()}
        side={isMobile ? "bottom" : "right"}
        panelClassName="h-[85dvh] sm:h-full w-full sm:max-w-xl border-t sm:border-t-0 sm:border-l border-border bg-canvas p-4 sm:p-6 shadow-sm"
        ariaLabel={editing ? "编辑角色" : "新增角色"}
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="font-content text-2xl text-ink">{editing ? "编辑角色" : "新增角色"}</div>
            <div className="mt-1 text-xs text-subtext">{dirty ? "未保存" : "已保存"}</div>
          </div>
          <div className="flex gap-2">
            <button className="btn btn-secondary" onClick={() => void closeDrawer()} type="button">
              关闭
            </button>
            <button
              className="btn btn-primary"
              disabled={saving || !form.name.trim()}
              onClick={() => void saveCharacter({ silent: false, close: true })}
              type="button"
            >
              保存
            </button>
          </div>
        </div>

        <div className="mt-5 grid gap-4">
          <label className="grid gap-1">
            <span className="text-xs text-subtext">姓名</span>
            <input
              className="input"
              name="name"
              value={form.name}
              onChange={(e) => setForm((v) => ({ ...v, name: e.target.value }))}
              placeholder="例如：林默"
            />
            <div className="text-[11px] text-subtext">建议使用读者容易记住的短名；后续会用于检索与生成。</div>
          </label>
          <label className="grid gap-1">
            <span className="text-xs text-subtext">角色定位</span>
            <input
              className="input"
              name="role"
              value={form.role}
              onChange={(e) => setForm((v) => ({ ...v, role: e.target.value }))}
              placeholder="例如：主角 / 反派 / 关键 NPC"
            />
            <div className="text-[11px] text-subtext">用于快速筛选；可以写“主角/反派/导师/同伴/路人”等。</div>
          </label>
          <label className="grid gap-1">
            <span className="text-xs text-subtext">人物档案</span>
            <textarea
              className="textarea atelier-content"
              name="profile"
              rows={8}
              value={form.profile}
              onChange={(e) => setForm((v) => ({ ...v, profile: e.target.value }))}
              placeholder="外貌、性格、动机、关系、口癖、成长线…"
            />
            <div className="text-[11px] text-subtext">用于生成时的角色一致性；可按条目写，更易复用。</div>
          </label>
          <label className="grid gap-1">
            <span className="text-xs text-subtext">备注</span>
            <textarea
              className="textarea atelier-content"
              name="notes"
              rows={6}
              value={form.notes}
              onChange={(e) => setForm((v) => ({ ...v, notes: e.target.value }))}
              placeholder="出场章节、禁忌、时间线、待补信息…"
            />
            <div className="text-[11px] text-subtext">记录未定稿/待补充信息，避免混进人物档案造成误导。</div>
          </label>
        </div>
      </Drawer>

      <WizardNextBar
        projectId={projectId}
        currentStep="characters"
        progress={wizard.progress}
        loading={wizard.loading}
        primaryAction={
          wizard.progress.nextStep?.key === "characters" ? { label: "本页：新增角色", onClick: openNew } : undefined
        }
      />
    </div>
  );
}
