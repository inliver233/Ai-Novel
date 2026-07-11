// @vitest-environment jsdom
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useChapterDetail } from "@/hooks/useChapterDetail";
import { ApiError } from "@/services/apiClient";
import type { ChapterDetail } from "@/types";

const mocks = vi.hoisted(() => ({
  fetchChapterDetail: vi.fn(),
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
      fetchAllChapterMeta: vi.fn(async () => []),
      fetchChapterDetail: mocks.fetchChapterDetail,
      updateChapter: vi.fn(),
    }),
  };
});

function chapter(id: string, content = "正文"): ChapterDetail {
  return {
    id,
    project_id: "project-preview",
    outline_id: "outline-preview",
    number: id === "chapter-b" ? 2 : 1,
    title: id === "chapter-b" ? "第二章" : "第一章",
    status: "done",
    updated_at: "2026-07-12T00:00:00Z",
    plan: "计划",
    summary: "摘要",
    content_md: content,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("useChapterDetail with real chapter store", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("contains an automatic initial rejection without toast or unhandled rejection", async () => {
    const unhandled = vi.fn();
    window.addEventListener("unhandledrejection", unhandled);
    const error = new ApiError({ code: "DETAIL_FAILED", message: "正文失败", requestId: "rid-detail", status: 503 });
    mocks.fetchChapterDetail.mockRejectedValueOnce(error);

    const { result } = renderHook(() =>
      useChapterDetail("chapter-initial-failure", { enabled: true, toastOnError: false }),
    );

    await waitFor(() => expect(result.current.error).toBe(error));
    await Promise.resolve();
    expect(result.current.hasLoaded).toBe(true);
    expect(result.current.hasData).toBe(false);
    expect(mocks.toast.toastError).not.toHaveBeenCalled();
    expect(unhandled).not.toHaveBeenCalled();
    window.removeEventListener("unhandledrejection", unhandled);
  });

  it("retains stale detail data when a forced refresh rejects", async () => {
    mocks.fetchChapterDetail.mockResolvedValueOnce(chapter("chapter-stale", "旧正文"));
    const { result } = renderHook(() => useChapterDetail("chapter-stale", { enabled: true, toastOnError: false }));
    await waitFor(() => expect(result.current.hasData).toBe(true));

    const error = new ApiError({
      code: "DETAIL_REFRESH_FAILED",
      message: "刷新失败",
      requestId: "rid-refresh",
      status: 503,
    });
    mocks.fetchChapterDetail.mockRejectedValueOnce(error);
    await act(async () => {
      await expect(result.current.refresh()).rejects.toBe(error);
    });

    expect(result.current.error).toBe(error);
    expect(result.current.chapter?.content_md).toBe("旧正文");
    expect(result.current.hasData).toBe(true);
    expect(mocks.toast.toastError).not.toHaveBeenCalled();
  });

  it("does not expose an old chapter failure after switching chapters", async () => {
    const oldRequest = deferred<ChapterDetail>();
    mocks.fetchChapterDetail.mockImplementation((chapterId: string) =>
      chapterId === "chapter-a" ? oldRequest.promise : Promise.resolve(chapter("chapter-b")),
    );
    const unhandled = vi.fn();
    window.addEventListener("unhandledrejection", unhandled);

    const { result, rerender } = renderHook(
      ({ chapterId }) => useChapterDetail(chapterId, { enabled: true, toastOnError: false }),
      { initialProps: { chapterId: "chapter-a" } },
    );
    rerender({ chapterId: "chapter-b" });
    await waitFor(() => expect(result.current.chapter?.id).toBe("chapter-b"));

    await act(async () => {
      oldRequest.reject(
        new ApiError({ code: "OLD_DETAIL_FAILED", message: "旧请求失败", requestId: "rid-old", status: 503 }),
      );
      await Promise.resolve();
    });

    expect(result.current.error).toBeNull();
    expect(result.current.chapter?.id).toBe("chapter-b");
    expect(unhandled).not.toHaveBeenCalled();
    window.removeEventListener("unhandledrejection", unhandled);
  });
});
