// @vitest-environment jsdom
import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useChapterMetaList } from "@/hooks/useChapterMetaList";
import { ApiError } from "@/services/apiClient";

const mocks = vi.hoisted(() => ({
  toast: { toastError: vi.fn() },
  snapshot: {
    data: null,
    error: null as ApiError | null,
    hasLoaded: true,
    loading: false,
    stale: true,
  },
}));

vi.mock("@/components/ui/toast", () => ({ useToast: () => mocks.toast }));
vi.mock("@/services/chapterStore", () => ({
  chapterStore: {
    subscribeMeta: () => () => undefined,
    getMetaSnapshot: () => mocks.snapshot,
    loadProjectChapterMeta: vi.fn(async () => []),
  },
}));

describe("useChapterMetaList toast policy", () => {
  it("lets the writing page own query error presentation", () => {
    mocks.toast.toastError.mockClear();
    mocks.snapshot.error = new ApiError({
      code: "CHAPTER_LIST_FAILED",
      message: "章节列表失败",
      requestId: "rid-list",
      status: 503,
    });

    const { result } = renderHook(() => useChapterMetaList("project-1", { toastOnError: false }));

    expect(result.current.error).toBe(mocks.snapshot.error);
    expect(result.current.hasData).toBe(false);
    expect(mocks.toast.toastError).not.toHaveBeenCalled();
  });
});
