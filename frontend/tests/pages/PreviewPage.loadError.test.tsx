// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PreviewPage } from "@/pages/PreviewPage";
import { ApiError } from "@/services/apiClient";
import type { ChapterDetail, ChapterListItem } from "@/types";

const mocks = vi.hoisted(() => ({
  listQuery: {
    chapters: [] as ChapterListItem[],
    error: null as ApiError | null,
    hasData: true,
    hasLoaded: true,
    loading: false,
    refresh: vi.fn(async () => [] as ChapterListItem[]),
    stale: false,
  },
  detailQuery: {
    chapter: null as ChapterDetail | null,
    error: null as ApiError | null,
    hasData: false,
    hasLoaded: false,
    loading: false,
    refresh: vi.fn(async () => null as ChapterDetail | null),
    stale: false,
  },
}));

vi.mock("@/hooks/useChapterMetaList", () => ({ useChapterMetaList: () => mocks.listQuery }));
vi.mock("@/hooks/useChapterDetail", () => ({ useChapterDetail: () => mocks.detailQuery }));
vi.mock("@/hooks/useWizardProgress", () => ({
  useWizardProgress: () => ({
    bumpLocal: vi.fn(),
    error: null,
    loading: false,
    progress: { percent: 50, steps: [], nextStep: null, exportedAt: null },
    reload: vi.fn(async () => undefined),
  }),
}));
vi.mock("react-router-dom", () => ({
  useParams: () => ({ projectId: "project-preview" }),
  useNavigate: () => vi.fn(),
}));
vi.mock("@/hooks/useIsMobile", () => ({ useIsMobile: () => false }));
vi.mock("@/services/chapterStore", () => ({
  chapterStore: { prefetchChapterDetail: vi.fn(async () => undefined) },
}));
vi.mock("@/services/wizard", () => ({ markWizardPreviewSeen: vi.fn() }));
vi.mock("@/components/layout/AppShell", () => ({
  PaperContent: (props: { children: React.ReactNode }) => <main>{props.children}</main>,
}));
vi.mock("@/components/writing/ChapterVirtualList", () => ({
  ChapterVirtualList: (props: {
    chapters: ChapterListItem[];
    emptyState?: React.ReactNode;
    onSelectChapter: (id: string) => void;
  }) =>
    props.chapters.length > 0 ? (
      <div>
        {props.chapters.map((chapter) => (
          <button key={chapter.id} onClick={() => props.onSelectChapter(chapter.id)} type="button">
            {chapter.title}
          </button>
        ))}
      </div>
    ) : (
      props.emptyState
    ),
}));
vi.mock("@/components/ui/Drawer", () => ({
  Drawer: (props: { children: React.ReactNode; open: boolean }) => (props.open ? props.children : null),
}));
vi.mock("@/components/atelier/WizardNextBar", () => ({ WizardNextBar: () => null }));

function chapterMeta(): ChapterListItem {
  return {
    id: "chapter-1",
    project_id: "project-preview",
    outline_id: "outline-preview",
    number: 1,
    title: "第一章",
    status: "done",
    updated_at: "2026-07-12T00:00:00Z",
    has_plan: true,
    has_summary: true,
    has_content: true,
  };
}

function chapterDetail(): ChapterDetail {
  return {
    ...chapterMeta(),
    plan: "计划",
    summary: "摘要",
    content_md: "保留的旧正文",
  };
}

describe("PreviewPage load errors", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.assign(mocks.listQuery, {
      chapters: [],
      error: null,
      hasData: true,
      hasLoaded: true,
      loading: false,
      stale: false,
    });
    Object.assign(mocks.detailQuery, {
      chapter: null,
      error: null,
      hasData: false,
      hasLoaded: false,
      loading: false,
      stale: false,
    });
  });

  it("shows a blocking list error without false empty states and contains retry rejection", async () => {
    const error = new ApiError({ code: "LIST_FAILED", message: "列表失败", requestId: "rid-list", status: 503 });
    Object.assign(mocks.listQuery, { error, hasData: false });
    mocks.listQuery.refresh.mockRejectedValueOnce(error);
    const unhandled = vi.fn();
    window.addEventListener("unhandledrejection", unhandled);

    render(<PreviewPage />);

    expect(screen.getByText("章节列表加载失败")).toBeInTheDocument();
    expect(screen.queryByText("暂无章节")).not.toBeInTheDocument();
    expect(screen.queryByText("暂无可预览内容")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(mocks.listQuery.refresh).toHaveBeenCalledTimes(1));
    await Promise.resolve();
    expect(unhandled).not.toHaveBeenCalled();
    window.removeEventListener("unhandledrejection", unhandled);
  });

  it("keeps stale chapter list content visible under a refresh warning", () => {
    Object.assign(mocks.listQuery, {
      chapters: [chapterMeta()],
      error: new ApiError({
        code: "LIST_REFRESH_FAILED",
        message: "刷新失败",
        requestId: "rid-list-refresh",
        status: 503,
      }),
      hasData: true,
    });

    render(<PreviewPage />);

    expect(screen.getByText("章节列表刷新失败")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "第一章" })).toBeInTheDocument();
  });

  it("keeps the selected chapter list and shows an inline detail retry instead of an empty body", () => {
    Object.assign(mocks.listQuery, { chapters: [chapterMeta()], hasData: true });
    Object.assign(mocks.detailQuery, {
      error: new ApiError({ code: "DETAIL_FAILED", message: "正文失败", requestId: "rid-detail", status: 503 }),
      hasData: false,
      hasLoaded: true,
    });

    render(<PreviewPage />);

    expect(screen.getByRole("button", { name: "第一章" })).toBeInTheDocument();
    expect(screen.getByText("章节正文加载失败")).toBeInTheDocument();
    expect(screen.queryByText("（空）")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(mocks.detailQuery.refresh).toHaveBeenCalledTimes(1);
  });

  it("keeps stale chapter body visible under a detail refresh warning", () => {
    Object.assign(mocks.listQuery, { chapters: [chapterMeta()], hasData: true });
    Object.assign(mocks.detailQuery, {
      chapter: chapterDetail(),
      error: new ApiError({
        code: "DETAIL_REFRESH_FAILED",
        message: "正文刷新失败",
        requestId: "rid-detail-refresh",
        status: 503,
      }),
      hasData: true,
      hasLoaded: true,
    });

    render(<PreviewPage />);

    expect(screen.getByText("章节正文刷新失败")).toBeInTheDocument();
    expect(screen.getByText("保留的旧正文")).toBeInTheDocument();
  });

  it("shows loading before the first detail snapshot instead of a false empty body", () => {
    Object.assign(mocks.listQuery, { chapters: [chapterMeta()], hasData: true });

    render(<PreviewPage />);

    expect(screen.getByText("章节加载中...")).toBeInTheDocument();
    expect(screen.queryByText("（空）")).not.toBeInTheDocument();
  });
});
