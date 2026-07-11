// @vitest-environment jsdom
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useChapterMetaList } from "@/hooks/useChapterMetaList";
import { ApiError } from "@/services/apiClient";

const mocks = vi.hoisted(() => ({
  fetchAllChapterMeta: vi.fn(),
  toast: { toastError: vi.fn() },
}));

vi.mock("@/components/ui/toast", () => ({ useToast: () => mocks.toast }));
vi.mock("@/services/chapterStore", async (importOriginal) => {
  const original = (await importOriginal()) as typeof import("@/services/chapterStore");
  return {
    ...original,
    chapterStore: original.createChapterStore({
      bulkCreateChapters: vi.fn(async () => []),
      createChapter: vi.fn(),
      deleteChapter: vi.fn(async () => undefined),
      fetchAllChapterMeta: mocks.fetchAllChapterMeta,
      fetchChapterDetail: vi.fn(),
      updateChapter: vi.fn(),
    }),
  };
});

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

describe("useChapterMetaList with real chapter store", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("contains automatic initial-load rejection in the snapshot without toast or unhandled rejection", async () => {
    const unhandled = vi.fn();
    window.addEventListener("unhandledrejection", unhandled);
    const error = new ApiError({
      code: "CHAPTER_LIST_FAILED",
      message: "章节列表失败",
      requestId: "rid-list",
      status: 503,
    });
    mocks.fetchAllChapterMeta.mockRejectedValue(error);

    const { result } = renderHook(() => useChapterMetaList("project-initial-failure", { toastOnError: false }));

    await waitFor(() => expect(result.current.error).toBe(error));
    await Promise.resolve();
    expect(result.current.hasData).toBe(false);
    expect(mocks.toast.toastError).not.toHaveBeenCalled();
    expect(unhandled).not.toHaveBeenCalled();
    window.removeEventListener("unhandledrejection", unhandled);
  });

  it("retains stale chapter data when a real-store refresh rejects", async () => {
    mocks.fetchAllChapterMeta.mockResolvedValueOnce([chapterMeta()]);
    const { result } = renderHook(() => useChapterMetaList("project-stale-failure", { toastOnError: false }));
    await waitFor(() => expect(result.current.hasData).toBe(true));

    const error = new ApiError({
      code: "CHAPTER_LIST_REFRESH_FAILED",
      message: "章节列表刷新失败",
      requestId: "rid-list-refresh",
      status: 503,
    });
    mocks.fetchAllChapterMeta.mockRejectedValue(error);

    await act(async () => {
      await expect(result.current.refresh()).rejects.toBe(error);
    });

    await waitFor(() => expect(result.current.error).toBe(error));
    expect(result.current.hasData).toBe(true);
    expect(result.current.chapters).toHaveLength(1);
    expect(mocks.toast.toastError).not.toHaveBeenCalled();
  });
});
