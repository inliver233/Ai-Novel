// @vitest-environment jsdom
import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useChapterMetaList } from "@/hooks/useChapterMetaList";
import { ApiError } from "@/services/apiClient";

const mocks = vi.hoisted(() => ({
  snapshot: {
    data: null,
    error: null as ApiError | null,
    hasLoaded: true,
    loading: false,
    stale: true,
  },
}));

vi.mock("@/services/chapterStore", () => ({
  chapterStore: {
    subscribeMeta: () => () => undefined,
    getMetaSnapshot: () => mocks.snapshot,
    loadProjectChapterMeta: vi.fn(async () => []),
  },
}));

describe("useChapterMetaList toast policy", () => {
  it("lets the writing page own query error presentation", () => {
    mocks.snapshot.error = new ApiError({
      code: "CHAPTER_LIST_FAILED",
      message: "章节列表失败",
      requestId: "rid-list",
      status: 503,
    });

    const { result } = renderHook(() => useChapterMetaList("project-1"));

    expect(result.current.error).toBe(mocks.snapshot.error);
    expect(result.current.hasData).toBe(false);
  });
});
