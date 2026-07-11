// @vitest-environment jsdom
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useOutlinePageState } from "@/pages/outline/useOutlinePageState";
import { ApiError } from "@/services/apiClient";

const mocks = vi.hoisted(() => ({
  apiJson: vi.fn(),
  failInitialLoad: true,
  toast: {
    toastSuccess: vi.fn(),
    toastWarning: vi.fn(),
    toastError: vi.fn(),
  },
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
  queuedSave: {
    save: vi.fn(async () => true),
    saving: false,
  },
  detailedOutline: {
    items: [],
    selected: null,
    generating: false,
    progress: null,
    skeletonGenerating: false,
    skeletonProgress: null,
    skeletonModalOpen: false,
    generateModalOpen: false,
    refresh: vi.fn(async () => undefined),
    generate: vi.fn(async () => true),
    openGenerateModal: vi.fn(),
    closeGenerateModal: vi.fn(),
    cancelGenerate: vi.fn(),
    openSkeletonModal: vi.fn(),
    closeSkeletonModal: vi.fn(),
    cancelSkeletonGenerate: vi.fn(),
  },
  generation: {
    open: false,
    generating: false,
    genPreview: null,
    genForm: {},
    setGenForm: vi.fn(),
    streamEnabled: false,
    setStreamEnabled: vi.fn(),
    streamProgress: null,
    streamPreviewJson: "",
    streamRawText: "",
    closeModal: vi.fn(),
    cancelGenerate: vi.fn(),
    generate: vi.fn(async () => true),
    clearPreview: vi.fn(),
    setOpen: vi.fn(),
    overwriteCurrentOutline: vi.fn(async () => true),
    saveAsNewOutline: vi.fn(async () => null),
  },
  parsing: {
    open: false,
    parsing: false,
    openParseModal: vi.fn(),
    parseProgress: null,
    parseForm: {},
    parseResult: null,
    agentCards: [],
    activeTab: "outline",
    closeParseModal: vi.fn(),
    cancelParse: vi.fn(),
    handleContentChange: vi.fn(),
    handleFileUpload: vi.fn(),
    handleAgentConfigChange: vi.fn(),
    startParse: vi.fn(),
    setActiveTab: vi.fn(),
    applyOutline: vi.fn(async () => ({ ok: true })),
    applyDetailedOutlines: vi.fn(async () => true),
    applyCharacters: vi.fn(),
    applyEntries: vi.fn(),
    applyAll: vi.fn(async () => ({ ok: true })),
  },
}));

vi.mock("@/services/apiClient", async (importOriginal) => ({
  ...((await importOriginal()) as object),
  apiJson: mocks.apiJson,
}));
vi.mock("@/services/wizard", () => ({
  markWizardProjectChanged: vi.fn(),
}));
vi.mock("@/components/ui/toast", () => ({ useToast: () => mocks.toast }));
vi.mock("@/components/ui/confirm", () => ({ useConfirm: () => mocks.confirm }));
vi.mock("react-router-dom", () => ({ useParams: () => ({ projectId: "project-outline" }) }));
vi.mock("@/hooks/usePersistentOutlet", () => ({ usePersistentOutletIsActive: () => false }));
vi.mock("@/hooks/useWizardProgress", () => ({ useWizardProgress: () => mocks.wizard }));
vi.mock("@/hooks/useQueuedSave", () => ({ useQueuedSave: () => mocks.queuedSave }));
vi.mock("@/hooks/useAutoSave", () => ({ useAutoSave: () => ({ cancel: vi.fn(), flush: vi.fn() }) }));
vi.mock("@/hooks/useSaveHotkey", () => ({ useSaveHotkey: () => undefined }));
vi.mock("@/pages/outline/useDetailedOutlineState", () => ({
  useDetailedOutlineState: () => mocks.detailedOutline,
}));
vi.mock("@/pages/outline/useOutlineGenerationState", () => ({
  useOutlineGenerationState: () => mocks.generation,
}));
vi.mock("@/pages/outline/useOutlineParsingState", () => ({
  useOutlineParsingState: () => mocks.parsing,
}));

describe("useOutlinePageState with real useProjectData", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.failInitialLoad = true;
    mocks.apiJson.mockImplementation(async (path: string) => {
      if (mocks.failInitialLoad && path.endsWith("/outline")) {
        throw new ApiError({
          code: "OUTLINE_LOAD_FAILED",
          message: "大纲加载失败",
          requestId: "rid-outline-real",
          status: 503,
        });
      }
      if (path.endsWith("/llm_preset")) return { data: { llm_preset: {} }, request_id: "rid-preset" };
      if (path.endsWith("/outlines")) {
        return {
          data: { outlines: [{ id: "outline-1", title: "主大纲", has_chapters: false }] },
          request_id: "rid-list",
        };
      }
      if (path.endsWith("/outline")) {
        return {
          data: {
            outline: { id: "outline-1", title: "主大纲", content_md: "# 已恢复的大纲", structure: null },
          },
          request_id: "rid-outline",
        };
      }
      throw new Error(`Unexpected request: ${path}`);
    });
  });

  it("surfaces request id without toast and recovers through the real query retry", async () => {
    const { result } = renderHook(() => useOutlinePageState());

    await waitFor(() => expect(result.current.blockingLoadError?.code).toBe("OUTLINE_LOAD_FAILED"));
    expect(result.current.blockingLoadError?.requestId).toBe("rid-outline-real");
    expect(mocks.toast.toastError).not.toHaveBeenCalled();

    mocks.failInitialLoad = false;
    await act(async () => {
      await result.current.reload();
    });

    await waitFor(() => expect(result.current.blockingLoadError).toBeNull());
    await waitFor(() => expect(result.current.editorProps.content).toBe("# 已恢复的大纲"));
    expect(result.current.refreshLoadError).toBeNull();
    expect(mocks.toast.toastError).not.toHaveBeenCalled();
  });
});
