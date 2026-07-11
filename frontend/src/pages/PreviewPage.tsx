import clsx from "clsx";
import { BookOpen, ChevronLeft, Edit3, List } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import { useNavigate, useParams } from "react-router-dom";
import remarkGfm from "remark-gfm";

import { WizardNextBar } from "../components/atelier/WizardNextBar";
import { PaperContent } from "../components/layout/AppShell";
import { ChapterVirtualList } from "../components/writing/ChapterVirtualList";
import { Drawer } from "../components/ui/Drawer";
import { useChapterDetail } from "../hooks/useChapterDetail";
import { useIsMobile } from "../hooks/useIsMobile";
import { useChapterMetaList } from "../hooks/useChapterMetaList";
import { useWizardProgress } from "../hooks/useWizardProgress";
import { chapterStore } from "../services/chapterStore";
import { markWizardPreviewSeen } from "../services/wizard";
import type { ChapterListItem } from "../types";

function humanizeChapterStatusZh(status: string): string {
  const s = String(status || "").trim();
  if (s === "planned") return "计划中";
  if (s === "drafting") return "草稿";
  if (s === "done") return "定稿";
  return s || "未知";
}

export function PreviewPage() {
  const { projectId } = useParams();
  const navigate = useNavigate();
  const isMobile = useIsMobile();
  const {
    bumpLocal,
    error: wizardError,
    loading: wizardLoading,
    progress: wizardProgress,
    reload: reloadWizard,
  } = useWizardProgress(projectId);

  const [activeId, setActiveId] = useState<string | null>(null);
  const [mobileListOpen, setMobileListOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [onlyDone, setOnlyDone] = useState(false);

  useEffect(() => {
    if (!projectId) return;
    markWizardPreviewSeen(projectId);
    bumpLocal();
  }, [bumpLocal, projectId]);

  const chapterListQuery = useChapterMetaList(projectId);
  const chapters = chapterListQuery.chapters as ChapterListItem[];
  const sortedChapters = useMemo(() => [...chapters].sort((a, b) => (a.number ?? 0) - (b.number ?? 0)), [chapters]);
  const doneCount = useMemo(
    () => sortedChapters.reduce((acc, c) => acc + (c.status === "done" ? 1 : 0), 0),
    [sortedChapters],
  );
  const visibleChapters = useMemo(() => {
    if (!onlyDone) return sortedChapters;
    return sortedChapters.filter((c) => c.status === "done");
  }, [onlyDone, sortedChapters]);

  const effectiveActiveId = useMemo(() => {
    if (activeId && visibleChapters.some((c) => c.id === activeId)) return activeId;
    return visibleChapters[0]?.id ?? null;
  }, [activeId, visibleChapters]);

  const activeIndex = useMemo(() => {
    if (!effectiveActiveId) return -1;
    return visibleChapters.findIndex((c) => c.id === effectiveActiveId);
  }, [effectiveActiveId, visibleChapters]);

  const activeChapterMeta = useMemo(() => {
    if (activeIndex < 0) return null;
    return visibleChapters[activeIndex] ?? null;
  }, [activeIndex, visibleChapters]);

  const prevChapter = useMemo(() => {
    if (activeIndex <= 0) return null;
    return visibleChapters[activeIndex - 1] ?? null;
  }, [activeIndex, visibleChapters]);

  const nextChapter = useMemo(() => {
    if (activeIndex < 0) return null;
    if (activeIndex >= visibleChapters.length - 1) return null;
    return visibleChapters[activeIndex + 1] ?? null;
  }, [activeIndex, visibleChapters]);

  const openEditor = (chapterId: string) => {
    if (!projectId) return;
    navigate(`/projects/${projectId}/writing?chapterId=${encodeURIComponent(chapterId)}`);
  };

  const openChapter = useCallback((chapterId: string) => {
    setActiveId(chapterId);
    setMobileListOpen(false);
  }, []);

  const { chapter: activeChapter, loading: loadingChapter } = useChapterDetail(effectiveActiveId, {
    enabled: Boolean(effectiveActiveId),
  });
  const activeChapterSummary = activeChapter ?? activeChapterMeta;

  useEffect(() => {
    if (prevChapter) void chapterStore.prefetchChapterDetail(prevChapter.id);
    if (nextChapter) void chapterStore.prefetchChapterDetail(nextChapter.id);
  }, [nextChapter, prevChapter]);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;

      const activeEl = document.activeElement;
      const isTypingTarget =
        activeEl instanceof HTMLElement &&
        (activeEl.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(activeEl.tagName));
      if (isTypingTarget) return;

      if (e.key === "ArrowLeft" && prevChapter) {
        e.preventDefault();
        openChapter(prevChapter.id);
        return;
      }
      if (e.key === "ArrowRight" && nextChapter) {
        e.preventDefault();
        openChapter(nextChapter.id);
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [nextChapter, openChapter, prevChapter]);

  const list = (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-3">
        <div className="inline-flex items-center gap-2 text-sm text-ink">
          <BookOpen size={16} />
          {"章节"}
        </div>
        <div className="flex items-center gap-2">
          <button
            className={clsx("btn btn-ghost px-2 py-1 text-xs", onlyDone ? "text-accent" : "text-subtext")}
            onClick={() => setOnlyDone((v) => !v)}
            type="button"
          >
            {onlyDone ? "显示全部" : "只看定稿"}
          </button>
          <span className="text-[11px] text-subtext">
            {doneCount}/{sortedChapters.length} {"已定稿"}
          </span>
        </div>
      </div>

      <div className="min-h-0 flex-1 p-2">
        <ChapterVirtualList
          chapters={visibleChapters}
          activeId={effectiveActiveId}
          ariaLabel="章节列表"
          className="h-full"
          emptyState={
            sortedChapters.length === 0 ? (
              <div className="p-3 text-sm text-subtext">{"暂无章节"}</div>
            ) : (
              <div className="p-3 text-sm text-subtext">{"暂无已定稿章节"}</div>
            )
          }
          getStatusLabel={(chapter) => humanizeChapterStatusZh(chapter.status)}
          onSelectChapter={openChapter}
          variant="card"
        />
      </div>
    </div>
  );

  if (!chapterListQuery.hasLoaded && chapterListQuery.loading) return <div className="text-subtext">加载中...</div>;

  return (
    <PaperContent className="grid gap-3 pb-[120px] sm:gap-4 sm:pb-24">
      {/* PC 端工具栏 */}
      <div className="hidden items-center justify-between gap-3 sm:flex">
        <div className="flex items-center gap-2">
          <button className="btn btn-ghost px-2 py-1 text-xs" onClick={() => navigate("/")} type="button">
            <ChevronLeft size={16} /> 返回首页
          </button>
          <button
            className="btn btn-secondary"
            disabled={!projectId}
            onClick={() => (projectId ? navigate(`/projects/${projectId}/writing`) : undefined)}
            type="button"
          >
            <ChevronLeft size={16} /> 返回写作
          </button>
          <button className="btn btn-secondary lg:hidden" onClick={() => setMobileListOpen(true)} type="button">
            <List size={16} /> 章节列表
          </button>
          <button
            className="btn btn-secondary hidden lg:inline-flex"
            onClick={() => setCollapsed((v) => !v)}
            type="button"
          >
            <List size={16} /> {collapsed ? "显示列表" : "隐藏列表"}
          </button>
        </div>

        <div className="min-w-0 truncate text-sm text-subtext">
          {activeChapterSummary ? `第 ${activeChapterSummary.number} 章` : "请选择章节"}
        </div>

        <div className="flex items-center gap-2">
          <button
            className="btn btn-secondary"
            disabled={!prevChapter}
            onClick={() => (prevChapter ? openChapter(prevChapter.id) : undefined)}
            type="button"
          >
            上一章
          </button>
          <button
            className="btn btn-secondary"
            disabled={!nextChapter}
            onClick={() => (nextChapter ? openChapter(nextChapter.id) : undefined)}
            type="button"
          >
            下一章
          </button>
          {activeChapterSummary ? (
            <button className="btn btn-secondary" onClick={() => openEditor(activeChapterSummary.id)} type="button">
              <Edit3 size={16} /> 编辑
            </button>
          ) : null}
          <span className="text-[11px] text-subtext">快捷键：← / →</span>
        </div>
      </div>

      {/* 手机端工具栏 */}
      <div className="flex items-center justify-between gap-2 sm:hidden">
        <button
          className="btn btn-secondary"
          disabled={!projectId}
          onClick={() => (projectId ? navigate(`/projects/${projectId}/writing`) : undefined)}
          type="button"
        >
          <ChevronLeft size={16} /> 返回
        </button>
        <div className="min-w-0 truncate text-xs text-subtext">
          {activeChapterSummary ? `第 ${activeChapterSummary.number} 章` : ""}
        </div>
        <button className="btn btn-secondary" onClick={() => setMobileListOpen(true)} type="button">
          <List size={16} /> 目录
        </button>
      </div>

      <div className="flex gap-4">
        {!collapsed ? (
          <aside className="hidden w-[260px] shrink-0 lg:block">
            <div className="panel h-[calc(100vh-220px)] min-h-[520px] overflow-hidden">{list}</div>
          </aside>
        ) : null}

        <section className="min-w-0 flex-1">
          <div className={clsx("panel", isMobile && "border-0 bg-canvas shadow-none")}>
            {activeChapterSummary ? (
              <>
                <div className="border-b border-border px-6 py-8 text-center sm:px-12 sm:py-10">
                  <div className="font-content text-2xl text-ink sm:text-3xl">第{activeChapterSummary.number}章</div>
                  {activeChapterSummary.title?.trim() ? (
                    <div className="mt-2 font-content text-lg text-subtext sm:text-xl">
                      {activeChapterSummary.title}
                    </div>
                  ) : null}
                  {activeChapterSummary.status !== "done" ? (
                    <div className="mt-2 text-xs text-subtext">
                      本章状态：{humanizeChapterStatusZh(activeChapterSummary.status)}
                    </div>
                  ) : null}
                </div>

                <div className="mx-auto max-w-[720px] px-5 py-8 sm:px-12 sm:py-10">
                  <div className="atelier-reader">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {loadingChapter ? "_(loading...)_" : activeChapter?.content_md || "_（空）_"}
                    </ReactMarkdown>
                  </div>
                </div>
              </>
            ) : (
              <div className="p-8 text-center text-subtext">暂无可预览内容</div>
            )}
          </div>
        </section>
      </div>

      {/* 手机端固定底部导航栏 */}
      <div className="fixed inset-x-0 bottom-24 z-20 flex items-center justify-between gap-2 border-t border-border bg-surface/80 px-4 py-3 shadow-sm backdrop-blur-sm sm:hidden mobile-safe-bottom">
        <button
          className="btn btn-secondary flex-1"
          disabled={!prevChapter}
          onClick={() => (prevChapter ? openChapter(prevChapter.id) : undefined)}
          type="button"
        >
          上一章
        </button>
        {activeChapterSummary ? (
          <button className="btn btn-ghost px-2" onClick={() => openEditor(activeChapterSummary.id)} type="button">
            <Edit3 size={16} />
          </button>
        ) : null}
        <button
          className="btn btn-secondary flex-1"
          disabled={!nextChapter}
          onClick={() => (nextChapter ? openChapter(nextChapter.id) : undefined)}
          type="button"
        >
          下一章
        </button>
      </div>

      <Drawer
        open={mobileListOpen}
        onClose={() => setMobileListOpen(false)}
        side="bottom"
        overlayClassName="lg:hidden"
        ariaLabel="章节列表"
        panelClassName="flex h-[85vh] w-full flex-col overflow-hidden rounded-atelier border border-border bg-surface shadow-sm"
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div className="text-sm text-ink">章节列表</div>
          <button className="btn btn-secondary" onClick={() => setMobileListOpen(false)} type="button">
            <ChevronLeft size={16} />
            关闭
          </button>
        </div>
        <div className="min-h-0 flex-1">{list}</div>
      </Drawer>

      <WizardNextBar
        projectId={projectId}
        currentStep="preview"
        progress={wizardProgress}
        loading={wizardLoading}
        loadError={wizardError}
        onRetryLoad={reloadWizard}
      />
    </PaperContent>
  );
}
