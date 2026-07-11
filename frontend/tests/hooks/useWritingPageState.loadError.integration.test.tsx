// @vitest-environment jsdom
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useWritingPageState } from "@/pages/writing/useWritingPageState";
import { ApiError } from "@/services/apiClient";

const mocks = vi.hoisted(() => ({
  apiJson: vi.fn(),
  listEntries: vi.fn(async () => ({ items: [], next_offset: null })),
  failMetadata: true,
  toast: { toastSuccess: vi.fn(), toastWarning: vi.fn(), toastError: vi.fn() },
  confirm: {
    confirm: vi.fn(async () => true),
    choose: vi.fn(async () => "cancel" as const),
  },
  wizard: {
    loading: false,
    progress: { steps: [], nextStep: null },
    refresh: vi.fn(async () => undefined),
    bumpLocal: vi.fn(),
  },
  chapterEditor: {
    loading: false,
    chapters: [],
    chapterListError: null as ApiError | null,
    chapterListHasData: true,
    chapterListHasLoaded: true,
    refreshChapters: vi.fn(async () => []),
    retryChapterList: vi.fn(async () => []),
    activeId: null,
    setActiveId: vi.fn(),
    activeChapter: null,
    baseline: null,
    form: null,
    setForm: vi.fn(),
    dirty: false,
    saveChapter: vi.fn(async () => true),
    requestSelectChapter: vi.fn(async () => undefined),
    loadingChapter: false,
    chapterLoadError: null,
    retryChapter: vi.fn(async () => undefined),
    saving: false,
  },
  chapterCrud: {
    createOpen: false,
    createSaving: false,
    createForm: {},
    setCreateForm: vi.fn(),
    setCreateOpen: vi.fn(),
    openCreate: vi.fn(),
    createChapter: vi.fn(async () => undefined),
    deleteChapter: vi.fn(async () => undefined),
  },
  generation: {
    generating: false,
    genRequestId: null,
    genStreamProgress: null,
    genForm: { stream: false },
    setGenForm: vi.fn(),
    generate: vi.fn(async () => undefined),
    abortGenerate: vi.fn(),
  },
  batch: {
    open: false,
    openModal: vi.fn(),
    closeModal: vi.fn(),
    batchLoading: false,
    batchCount: 1,
    setBatchCount: vi.fn(),
    batchIncludeExisting: false,
    setBatchIncludeExisting: vi.fn(),
    batchTask: null,
    batchItems: [],
    batchRuntime: null,
    projectTaskStreamStatus: "idle",
    cancelBatchGeneration: vi.fn(),
    pauseBatchGeneration: vi.fn(),
    resumeBatchGeneration: vi.fn(),
    retryFailedBatchGeneration: vi.fn(),
    skipFailedBatchGeneration: vi.fn(),
    startBatchGeneration: vi.fn(),
    applyBatchItemToEditor: vi.fn(),
  },
  history: {
    open: false,
    openDrawer: vi.fn(),
    closeDrawer: vi.fn(),
    runsLoading: false,
    runs: [],
    selectedRun: null,
    selectRun: vi.fn(async () => undefined),
  },
}));

vi.mock("@/services/apiClient", async (importOriginal) => ({
  ...((await importOriginal()) as object),
  apiJson: mocks.apiJson,
}));
vi.mock("@/services/entriesApi", () => ({ listEntries: mocks.listEntries }));
vi.mock("react-router-dom", () => ({
  useParams: () => ({ projectId: "project-writing" }),
  useSearchParams: () => [new URLSearchParams(), vi.fn()],
}));
vi.mock("@/components/ui/toast", () => ({ useToast: () => mocks.toast }));
vi.mock("@/components/ui/confirm", () => ({ useConfirm: () => mocks.confirm }));
vi.mock("@/hooks/usePersistentOutlet", () => ({ usePersistentOutletIsActive: () => false }));
vi.mock("@/hooks/useWizardProgress", () => ({ useWizardProgress: () => mocks.wizard }));
vi.mock("@/services/wizard", () => ({ getWizardProjectChangedAt: () => null }));
vi.mock("@/pages/writing/useChapterEditor", () => ({ useChapterEditor: () => mocks.chapterEditor }));
vi.mock("@/pages/writing/useApplyGenerationRun", () => ({ useApplyGenerationRun: () => undefined }));
vi.mock("@/pages/writing/useChapterCrud", () => ({ useChapterCrud: () => mocks.chapterCrud }));
vi.mock("@/pages/writing/useChapterGeneration", () => ({ useChapterGeneration: () => mocks.generation }));
vi.mock("@/pages/writing/useBatchGeneration", () => ({ useBatchGeneration: () => mocks.batch }));
vi.mock("@/pages/writing/useGenerationHistory", () => ({ useGenerationHistory: () => mocks.history }));
vi.mock("@/pages/writing/useOutlineSwitcher", () => ({ useOutlineSwitcher: () => vi.fn() }));

describe("useWritingPageState with real useProjectData", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.failMetadata = true;
    mocks.chapterEditor.chapterListError = null;
    mocks.chapterEditor.chapterListHasData = true;
    mocks.chapterEditor.retryChapterList.mockResolvedValue([]);
    mocks.apiJson.mockImplementation(async (path: string) => {
      if (mocks.failMetadata && path.endsWith("/outline")) {
        throw new ApiError({
          code: "WRITING_LOAD_FAILED",
          message: "写作元数据加载失败",
          requestId: "rid-writing-real",
          status: 503,
        });
      }
      if (path.endsWith("/outline")) {
        return {
          data: { outline: { id: "outline-1", title: "主大纲", content_md: "# 大纲", structure: null } },
          request_id: "rid-outline",
        };
      }
      if (path.endsWith("/outlines")) {
        return {
          data: { outlines: [{ id: "outline-1", title: "主大纲", has_chapters: false }] },
          request_id: "rid-outlines",
        };
      }
      if (path.endsWith("/llm_preset")) return { data: { llm_preset: {} }, request_id: "rid-preset" };
      if (path.endsWith("/characters")) return { data: { characters: [] }, request_id: "rid-characters" };
      throw new Error(`Unexpected request: ${path}`);
    });
  });

  it("recovers metadata without query toast and preserves stale data on a later refresh failure", async () => {
    const { result, rerender } = renderHook(() => useWritingPageState());

    await waitFor(() => expect(result.current.metadataBlockingLoadError?.code).toBe("WRITING_LOAD_FAILED"));
    expect(result.current.metadataBlockingLoadError?.requestId).toBe("rid-writing-real");
    expect(mocks.toast.toastError).not.toHaveBeenCalled();

    mocks.failMetadata = false;
    await act(async () => {
      await result.current.reloadMetadata();
    });
    await waitFor(() => expect(result.current.metadataBlockingLoadError).toBeNull());
    expect(result.current.workspaceProps.toolbarProps.outlines).toHaveLength(1);

    mocks.failMetadata = true;
    await act(async () => {
      await result.current.reloadMetadata();
    });

    expect(result.current.loading).toBe(false);
    expect(result.current.metadataRefreshLoadError?.code).toBe("WRITING_LOAD_FAILED");
    expect(result.current.workspaceProps.toolbarProps.outlines).toHaveLength(1);
    expect(mocks.toast.toastError).not.toHaveBeenCalled();

    const listError = new ApiError({
      code: "CHAPTER_LIST_REFRESH_FAILED",
      message: "章节列表刷新失败",
      requestId: "rid-list-refresh",
      status: 503,
    });
    mocks.chapterEditor.chapterListError = listError;
    mocks.chapterEditor.chapterListHasData = true;
    mocks.chapterEditor.retryChapterList.mockRejectedValueOnce(listError);
    rerender();

    expect(result.current.chapterListRefreshLoadError).toBe(listError);
    await expect(result.current.reloadChapterList()).resolves.toBeUndefined();
    expect(result.current.chapterListRefreshLoadError).toBe(listError);
    expect(mocks.toast.toastError).not.toHaveBeenCalled();
  });
});
