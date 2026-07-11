// @vitest-environment jsdom
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useWizardProgress } from "@/hooks/useWizardProgress";
import { ApiError } from "@/services/apiClient";

const mocks = vi.hoisted(() => ({
  apiJson: vi.fn(),
  failWizard: true,
  toast: { toastError: vi.fn() },
  chapterQuery: {
    chapters: [],
    error: null as ApiError | null,
    hasData: true,
    hasLoaded: true,
    loading: false,
    refresh: vi.fn(async () => []),
    stale: false,
  },
}));

vi.mock("@/services/apiClient", async (importOriginal) => ({
  ...((await importOriginal()) as object),
  apiJson: mocks.apiJson,
}));
vi.mock("@/components/ui/toast", () => ({ useToast: () => mocks.toast }));
vi.mock("@/hooks/useChapterMetaList", () => ({ useChapterMetaList: () => mocks.chapterQuery }));
vi.mock("@/services/wizard", async (importOriginal) => ({
  ...((await importOriginal()) as object),
  onWizardProgressInvalidated: () => () => undefined,
}));

describe("useWizardProgress load errors", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.failWizard = true;
    mocks.chapterQuery.error = null;
    mocks.chapterQuery.hasData = true;
    mocks.chapterQuery.loading = false;
    mocks.chapterQuery.refresh.mockResolvedValue([]);
    mocks.apiJson.mockImplementation(async (path: string) => {
      if (mocks.failWizard && path.endsWith("/settings")) {
        throw new ApiError({
          code: "WIZARD_LOAD_FAILED",
          message: "向导加载失败",
          requestId: "rid-wizard",
          status: 503,
        });
      }
      if (/\/api\/projects\/[^/]+$/.test(path)) {
        return { data: { project: { id: "project-1", name: "项目", llm_profile_id: null } }, request_id: "rid-p" };
      }
      if (path.endsWith("/settings")) return { data: { settings: {} }, request_id: "rid-settings" };
      if (path.endsWith("/characters")) return { data: { characters: [] }, request_id: "rid-characters" };
      if (path.endsWith("/outline")) {
        return { data: { outline: { id: "outline-1", content_md: "", structure: null } }, request_id: "rid-outline" };
      }
      if (path.endsWith("/llm_preset")) return { data: { llm_preset: {} }, request_id: "rid-preset" };
      if (path === "/api/llm_profiles") return { data: { profiles: [] }, request_id: "rid-profiles" };
      throw new Error(`Unexpected request: ${path}`);
    });
  });

  it("surfaces wizard and chapter errors without toast and recovers through reload", async () => {
    const { result, rerender } = renderHook(() => useWizardProgress("project-1"));

    await waitFor(() => expect(result.current.blockingError?.code).toBe("WIZARD_LOAD_FAILED"));
    expect(result.current.error?.requestId).toBe("rid-wizard");
    expect(mocks.toast.toastError).not.toHaveBeenCalled();

    mocks.failWizard = false;
    await act(async () => {
      await result.current.reload();
    });
    await waitFor(() => expect(result.current.blockingError).toBeNull());

    const chapterError = new ApiError({
      code: "CHAPTER_PROGRESS_FAILED",
      message: "章节进度失败",
      requestId: "rid-chapter-progress",
      status: 503,
    });
    mocks.chapterQuery.hasData = false;
    mocks.chapterQuery.error = chapterError;
    rerender();
    expect(result.current.blockingError).toBe(chapterError);

    mocks.chapterQuery.hasData = true;
    rerender();
    expect(result.current.refreshError).toBe(chapterError);

    mocks.chapterQuery.refresh.mockImplementationOnce(async () => {
      mocks.chapterQuery.error = null;
      return [];
    });
    await act(async () => {
      await result.current.reload();
    });
    rerender();
    expect(result.current.error).toBeNull();
    expect(mocks.toast.toastError).not.toHaveBeenCalled();
  });
});
