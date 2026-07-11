// @vitest-environment jsdom
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useChapterEditor } from "@/pages/writing/useChapterEditor";
import { ApiError } from "@/services/apiClient";
import type { ChapterListItem } from "@/types";

const mocks = vi.hoisted(() => ({
  chapterQuery: {
    chapters: [] as Array<{
      id: string;
      project_id: string;
      outline_id: string;
      number: number;
      title: string;
      status: "planned" | "drafting" | "done";
      updated_at: string;
      has_plan: boolean;
      has_summary: boolean;
      has_content: boolean;
    }>,
    error: null as ApiError | null,
    hasData: true,
    hasLoaded: true,
    loading: false,
    refresh: vi.fn(async (): Promise<ChapterListItem[]> => []),
    stale: false,
  },
  loadChapterDetail: vi.fn(),
  toast: {
    toastSuccess: vi.fn(),
    toastWarning: vi.fn(),
    toastError: vi.fn(),
  },
  confirm: {
    confirm: vi.fn(async () => true),
    choose: vi.fn(async () => "cancel" as const),
  },
}));

vi.mock("@/hooks/useChapterMetaList", () => ({ useChapterMetaList: () => mocks.chapterQuery }));
vi.mock("@/services/chapterStore", () => ({
  chapterStore: {
    loadChapterDetail: mocks.loadChapterDetail,
    updateChapterDetail: vi.fn(),
  },
}));
vi.mock("@/hooks/useAutoSave", () => ({ useAutoSave: () => ({ cancel: vi.fn(), flush: vi.fn() }) }));
vi.mock("@/hooks/useSaveHotkey", () => ({ useSaveHotkey: () => undefined }));
vi.mock("@/services/wizard", () => ({ markWizardProjectChanged: vi.fn() }));

function chapterMeta() {
  return {
    id: "chapter-1",
    project_id: "project-1",
    outline_id: "outline-1",
    number: 1,
    title: "第一章",
    status: "drafting" as const,
    updated_at: "2026-07-12T00:00:00Z",
    has_plan: true,
    has_summary: true,
    has_content: true,
  };
}

function chapterDetail() {
  return {
    ...chapterMeta(),
    plan: "计划",
    content_md: "正文",
    summary: "摘要",
  };
}

function renderEditor() {
  return renderHook(() =>
    useChapterEditor({
      projectId: "project-1",
      requestedChapterId: null,
      searchParams: new URLSearchParams(),
      setSearchParams: vi.fn(),
      toast: mocks.toast,
      confirm: mocks.confirm,
      refreshWizard: vi.fn(async () => undefined),
      bumpWizardLocal: vi.fn(),
    }),
  );
}

describe("useChapterEditor load errors", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.chapterQuery.chapters = [chapterMeta()];
    mocks.chapterQuery.error = null;
    mocks.chapterQuery.hasData = true;
    mocks.chapterQuery.hasLoaded = true;
    mocks.chapterQuery.loading = false;
  });

  it("exposes chapter-list failure and delegates force retry without query toast", async () => {
    const error = new ApiError({
      code: "CHAPTER_LIST_FAILED",
      message: "章节列表失败",
      requestId: "rid-list",
      status: 503,
    });
    mocks.chapterQuery.chapters = [];
    mocks.chapterQuery.error = error;
    mocks.chapterQuery.hasData = false;
    mocks.chapterQuery.refresh.mockResolvedValueOnce([chapterMeta()]);

    const { result } = renderEditor();

    expect(result.current.chapterListError).toBe(error);
    expect(result.current.chapterListHasData).toBe(false);
    await act(async () => {
      await result.current.retryChapterList();
    });
    expect(mocks.chapterQuery.refresh).toHaveBeenCalledTimes(1);
    expect(mocks.toast.toastError).not.toHaveBeenCalled();
  });

  it("keeps the chapter list and recovers an inline detail error through force retry", async () => {
    const error = new ApiError({
      code: "CHAPTER_DETAIL_FAILED",
      message: "章节正文加载失败",
      requestId: "rid-detail",
      status: 503,
    });
    mocks.loadChapterDetail.mockRejectedValueOnce(error).mockResolvedValueOnce(chapterDetail());

    const { result } = renderEditor();

    await waitFor(() => expect(result.current.chapterLoadError).toBe(error));
    expect(result.current.chapters).toHaveLength(1);
    expect(result.current.activeChapter).toBeNull();
    expect(mocks.toast.toastError).not.toHaveBeenCalled();

    await act(async () => {
      await result.current.retryChapter();
    });

    expect(mocks.loadChapterDetail).toHaveBeenLastCalledWith("chapter-1", { force: true });
    expect(result.current.chapterLoadError).toBeNull();
    expect(result.current.activeChapter?.id).toBe("chapter-1");
    expect(result.current.form?.content_md).toBe("正文");
    expect(result.current.chapters).toHaveLength(1);
  });
});
