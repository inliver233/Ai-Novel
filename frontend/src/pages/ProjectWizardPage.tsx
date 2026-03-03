import clsx from "clsx";
import { motion, useReducedMotion } from "framer-motion";
import { CheckCircle2, Circle, CircleSlash2, Wand2 } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { GhostwriterIndicator } from "../components/atelier/GhostwriterIndicator";
import { WizardNextBar } from "../components/atelier/WizardNextBar";
import { ProgressBar } from "../components/ui/ProgressBar";
import { useConfirm } from "../components/ui/confirm";
import { useToast } from "../components/ui/toast";
import { useProjects } from "../contexts/projects";
import { useProjectData } from "../hooks/useProjectData";
import { duration, transition } from "../lib/motion";
import { UI_COPY } from "../lib/uiCopy";
import { ApiError, apiJson } from "../services/apiClient";
import { computeWizardProgress, setWizardStepSkipped, type WizardStep, type WizardStepKey } from "../services/wizard";
import type { Chapter, Character, LLMPreset, LLMProfile, Outline, ProjectSettings } from "../types";

type OutlineGenChapter = { number: number; title: string; beats: string[] };
type OutlineGenResult = {
  outline_md: string;
  chapters: OutlineGenChapter[];
  raw_output: string;
  parse_error?: { code: string; message: string };
};

type WizardLoaded = {
  settings: ProjectSettings;
  characters: Character[];
  outline: Outline;
  chapters: Chapter[];
  llmPreset: LLMPreset;
  profiles: LLMProfile[];
};

const EMPTY_CHARACTERS: Character[] = [];
const EMPTY_CHAPTERS: Chapter[] = [];
const EMPTY_PROFILES: LLMProfile[] = [];

export function ProjectWizardPage() {
  const { projectId } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const confirm = useConfirm();
  const reduceMotion = useReducedMotion();
  const { projects } = useProjects();

  const project = useMemo(() => projects.find((p) => p.id === projectId) ?? null, [projectId, projects]);

  const [version, setVersion] = useState(0);
  const [autoRunning, setAutoRunning] = useState(false);

  const wizardQuery = useProjectData<WizardLoaded>(projectId, async (id) => {
    const [settingsRes, charsRes, outlineRes, presetRes, profilesRes] = await Promise.all([
      apiJson<{ settings: ProjectSettings }>(`/api/projects/${id}/settings`),
      apiJson<{ characters: Character[] }>(`/api/projects/${id}/characters`),
      apiJson<{ outline: Outline }>(`/api/projects/${id}/outline`),
      apiJson<{ llm_preset: LLMPreset }>(`/api/projects/${id}/llm_preset`),
      apiJson<{ profiles: LLMProfile[] }>(`/api/llm_profiles`),
    ]);
    const chaptersRes = await apiJson<{ chapters: Chapter[] }>(`/api/projects/${id}/chapters`);
    return {
      settings: settingsRes.data.settings,
      characters: charsRes.data.characters,
      outline: outlineRes.data.outline,
      chapters: chaptersRes.data.chapters,
      llmPreset: presetRes.data.llm_preset,
      profiles: profilesRes.data.profiles,
    };
  });

  const reload = wizardQuery.refresh;
  const settings = wizardQuery.data?.settings ?? null;
  const characters = wizardQuery.data?.characters ?? EMPTY_CHARACTERS;
  const outline = wizardQuery.data?.outline ?? null;
  const chapters = wizardQuery.data?.chapters ?? EMPTY_CHAPTERS;
  const llmPreset = wizardQuery.data?.llmPreset ?? null;
  const profiles = wizardQuery.data?.profiles ?? EMPTY_PROFILES;

  const progress = useMemo(() => {
    void version;
    const selectedProfileId = project?.llm_profile_id ?? null;
    const llmProfile = selectedProfileId ? (profiles.find((p) => p.id === selectedProfileId) ?? null) : null;
    return computeWizardProgress({
      project,
      settings,
      characters,
      outline,
      chapters,
      llmPreset,
      llmProfile,
    });
  }, [project, settings, characters, outline, chapters, llmPreset, profiles, version]);

  const goStep = useCallback(
    (step: WizardStep) => {
      if (!step.href) return;
      navigate(step.href);
    },
    [navigate],
  );

  const setSkipped = useCallback(
    (step: WizardStepKey, skipped: boolean) => {
      if (!projectId) return;
      setWizardStepSkipped(projectId, step, skipped);
      setVersion((v) => v + 1);
    },
    [projectId],
  );

  const scrollToSteps = useCallback(() => {
    const el = document.getElementById("wizard-steps");
    el?.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
  }, [reduceMotion]);

  const autoOutlineAndChapters = useCallback(async () => {
    if (!projectId) return;
    if (!llmPreset) {
      toast.toastError(`未加载到模型配置，请先在「${UI_COPY.nav.prompts}」页保存模型预设`);
      navigate(`/projects/${projectId}/prompts`);
      return;
    }
    const headers: Record<string, string> = { "X-LLM-Provider": llmPreset.provider };

    const ok = await confirm.confirm({
      title: "自动生成大纲并创建章节骨架？",
      description: "将调用 LLM 生成大纲，保存为新大纲版本，并创建章节骨架。",
      confirmText: "开始",
    });
    if (!ok) return;

    setAutoRunning(true);
    try {
      const outlineGen = await apiJson<OutlineGenResult>(`/api/projects/${projectId}/outline/generate`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          requirements: {
            chapter_count: 12,
            tone: "偏现实，克制但有爆点",
            pacing: "前3章强钩子，中段升级，结尾反转",
          },
          context: {
            include_world_setting: true,
            include_characters: true,
          },
        }),
      });

      const outlineMd = outlineGen.data.outline_md ?? "";
      const genChapters = outlineGen.data.chapters ?? [];
      if (genChapters.length === 0) {
        toast.toastError("已生成大纲，但未解析出章节结构；请到大纲页手动调整并创建章节。");
        navigate(`/projects/${projectId}/outline`);
        return;
      }

      await apiJson<{ outline: Outline }>(`/api/projects/${projectId}/outlines`, {
        method: "POST",
        body: JSON.stringify({
          title: `AI 大纲 ${new Date().toISOString().slice(0, 16).replace("T", " ")}`,
          content_md: outlineMd,
          structure: { chapters: genChapters },
        }),
      });

      const payload = {
        chapters: genChapters.map((c) => ({
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
      } catch (e) {
        const err = e as ApiError;
        if (err.code === "CONFLICT" && err.status === 409) {
          const replaceOk = await confirm.confirm({
            title: "检测到已有章节，是否继续覆盖？",
            description: `覆盖创建将永久删除当前大纲下所有章节（含正文/摘要，约 ${chapters.length} 章），且无法撤销。`,
            confirmText: "继续覆盖",
            danger: true,
          });
          if (!replaceOk) return;
          const doubleCheckOk = await confirm.confirm({
            title: "最后确认：覆盖章节并创建骨架？",
            description: "此操作不可恢复。若你只是想保留已有章节，请点击取消返回。",
            confirmText: "我已知晓，继续覆盖",
            danger: true,
          });
          if (!doubleCheckOk) return;
          await apiJson<{ chapters: Chapter[] }>(`/api/projects/${projectId}/chapters/bulk_create?replace=true`, {
            method: "POST",
            body: JSON.stringify(payload),
          });
        } else {
          throw e;
        }
      }

      toast.toastSuccess("已生成大纲并创建章节骨架");
      navigate(`/projects/${projectId}/writing`);
    } catch (e) {
      const err = e as ApiError;
      toast.toastError(`${err.message} (${err.code})`, err.requestId);
    } finally {
      setAutoRunning(false);
    }
  }, [chapters.length, confirm, llmPreset, navigate, projectId, toast]);

  if (!projectId) {
    return (
      <div className="panel p-6">
        <div className="font-content text-xl text-ink">缺少项目 ID</div>
        <div className="mt-2 text-sm text-subtext">请从首页选择一个项目后再进入开工向导。</div>
        <button className="btn btn-secondary mt-4" onClick={() => navigate("/")} type="button">
          返回首页
        </button>
      </div>
    );
  }
  if (wizardQuery.loading) {
    return (
      <div className="panel p-6">
        <div className="text-sm text-subtext">正在加载向导数据...</div>
      </div>
    );
  }

  const nextStep = progress.nextStep;

  return (
    <div className="grid gap-6 pb-[calc(6rem+env(safe-area-inset-bottom))]">
      <section className="panel p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="grid gap-2">
            <div className="font-content text-xl">开工向导</div>
            <div className="text-xs text-subtext">
              {project ? (
                <>
                  {UI_COPY.nav.currentProject}：<span className="text-ink">{project.name}</span>
                  <span className="mx-2 text-subtext/60">·</span>
                  按步骤跑通闭环：{UI_COPY.nav.projectSettings} → {UI_COPY.nav.characters} → {UI_COPY.nav.prompts} →{" "}
                  {UI_COPY.nav.outline} → {UI_COPY.nav.writing} → {UI_COPY.nav.preview} → {UI_COPY.nav.export}
                </>
              ) : (
                <>
                  按步骤跑通闭环：{UI_COPY.nav.projectSettings} → {UI_COPY.nav.characters} → {UI_COPY.nav.prompts} →{" "}
                  {UI_COPY.nav.outline} → {UI_COPY.nav.writing} → {UI_COPY.nav.preview} → {UI_COPY.nav.export}
                </>
              )}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button className="btn btn-secondary" onClick={() => void reload()} type="button">
              刷新进度
            </button>
            <div className="rounded-atelier border border-border bg-canvas px-3 py-2 text-xs text-subtext">
              {nextStep ? `下一步：${nextStep.title}` : "已完成"}
            </div>
          </div>
        </div>

        <div className="mt-4">
          <ProgressBar ariaLabel="项目开工向导完成度" value={progress.percent} />
          <div className="mt-2 text-xs text-subtext">完成度：{progress.percent}%</div>
        </div>
      </section>

      <section className="panel p-6">
        <div className="grid gap-1">
          <div className="font-content text-xl">从这里开始</div>
          <div className="text-xs text-subtext">请选择「按步骤（推荐）」或「快速开工（自动）」；两者都可随时切换。</div>
        </div>
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          <div className="surface p-4">
            <div className="grid gap-2">
              <div className="flex items-center gap-2">
                <div className="font-content text-base text-ink">按步骤（推荐）</div>
                <div className="rounded-atelier bg-accent/15 px-2 py-0.5 text-[11px] text-accent">推荐</div>
              </div>
              <div className="text-xs text-subtext">
                按顺序跑通闭环：{UI_COPY.nav.projectSettings} → {UI_COPY.nav.characters} → {UI_COPY.nav.prompts} →{" "}
                {UI_COPY.nav.outline} → {UI_COPY.nav.writing} → {UI_COPY.nav.preview} → {UI_COPY.nav.export}
              </div>
              <div className="text-xs text-subtext">{nextStep ? `下一步：${nextStep.title}` : "已完成全部步骤"}</div>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              {nextStep ? (
                <button className="btn btn-primary" onClick={() => goStep(nextStep)} type="button">
                  开始下一步
                </button>
              ) : (
                <button className="btn btn-primary" onClick={() => navigate("/")} type="button">
                  回到项目概览
                </button>
              )}
              <button className="btn btn-secondary" onClick={scrollToSteps} type="button">
                查看步骤清单
              </button>
            </div>
          </div>
          <div className="surface p-4">
            <div className="grid gap-2">
              <div className="font-content text-base text-ink">快速开工（自动）</div>
              <div className="text-xs text-subtext">一键：生成大纲 → 保存 → 创建章节骨架 → 跳转写作页。</div>
              <div className="text-xs text-subtext">
                建议先完成「{UI_COPY.nav.projectSettings} / {UI_COPY.nav.prompts}」，以避免生成失败。
              </div>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <button
                className="btn btn-primary"
                disabled={autoRunning}
                onClick={() => void autoOutlineAndChapters()}
                type="button"
              >
                <span className="inline-flex items-center gap-2">
                  <Wand2 size={18} />
                  {autoRunning ? "运行中..." : "一键开工"}
                </span>
              </button>
              <button className="btn btn-secondary" onClick={scrollToSteps} type="button">
                改用按步骤
              </button>
            </div>
          </div>
        </div>
        {autoRunning ? <GhostwriterIndicator className="mt-4" label="正在调用模型生成大纲与章节结构…" /> : null}
      </section>

      <section className="panel p-6" id="wizard-steps">
        <div className="grid gap-1">
          <div className="font-content text-xl">步骤清单</div>
          <div className="text-xs text-subtext">从上到下完成；不适用的步骤可以先跳过，之后也可取消跳过。</div>
        </div>
        <motion.div
          className="mt-4 grid gap-3"
          initial="hidden"
          animate="show"
          variants={{
            hidden: {},
            show: { transition: { staggerChildren: reduceMotion ? 0 : duration.stagger } },
          }}
        >
          {progress.steps.map((s) => {
            const Icon = s.state === "done" ? CheckCircle2 : s.state === "skipped" ? CircleSlash2 : Circle;
            const badge =
              s.state === "done"
                ? "已完成"
                : s.state === "skipped"
                  ? "已跳过"
                  : progress.nextStep?.key === s.key
                    ? "下一步"
                    : "待完成";
            return (
              <motion.div
                key={s.key}
                className="surface p-4"
                initial="hidden"
                animate="show"
                variants={{
                  hidden: reduceMotion ? { opacity: 0 } : { opacity: 0, y: 8 },
                  show: reduceMotion ? { opacity: 1 } : { opacity: 1, y: 0 },
                }}
                transition={reduceMotion ? { duration: 0.01 } : transition.base}
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <Icon
                      className={clsx(
                        "shrink-0",
                        s.state === "done" ? "text-success" : s.state === "skipped" ? "text-subtext" : "text-subtext",
                      )}
                      size={18}
                    />
                    <div className="min-w-0 truncate text-sm text-ink">{s.title}</div>
                    <div
                      className={clsx(
                        "shrink-0 rounded-atelier px-2 py-0.5 text-[11px]",
                        s.state === "done"
                          ? "bg-success/15 text-success"
                          : s.state === "skipped"
                            ? "bg-border/60 text-subtext"
                            : progress.nextStep?.key === s.key
                              ? "bg-accent/15 text-accent"
                              : "bg-border/60 text-subtext",
                      )}
                    >
                      {badge}
                    </div>
                  </div>
                  <div className="mt-1 text-xs text-subtext">{s.description}</div>
                </div>
                <div className="flex shrink-0 flex-wrap gap-2">
                  <button className="btn btn-secondary" onClick={() => goStep(s)} type="button">
                    前往
                  </button>
                  {s.state === "todo" ? (
                    <button
                      className="btn btn-secondary text-subtext"
                      onClick={() => setSkipped(s.key, true)}
                      type="button"
                    >
                      跳过
                    </button>
                  ) : s.state === "skipped" ? (
                    <button
                      className="btn btn-secondary text-subtext"
                      onClick={() => setSkipped(s.key, false)}
                      type="button"
                    >
                      取消跳过
                    </button>
                  ) : null}
                </div>
              </motion.div>
            );
          })}
        </motion.div>
      </section>

      <WizardNextBar
        projectId={projectId}
        currentStep={nextStep?.key ?? "export"}
        progress={progress}
        primaryAction={
          nextStep
            ? {
                label: `下一步：${nextStep.title}`,
                onClick: () => goStep(nextStep),
              }
            : {
                label: "已完成：回到项目概览",
                onClick: () => navigate("/"),
              }
        }
      />
    </div>
  );
}
