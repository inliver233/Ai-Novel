// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OutlinePage } from "@/pages/OutlinePage";
import { deriveOutlinePageLoadState, type OutlinePageState } from "@/pages/outline/useOutlinePageState";
import { ApiError } from "@/services/apiClient";

const mocks = vi.hoisted(() => ({
  useOutlinePageState: vi.fn(),
}));

vi.mock("@/pages/outline/useOutlinePageState", async (importOriginal) => ({
  ...((await importOriginal()) as object),
  useOutlinePageState: mocks.useOutlinePageState,
}));
vi.mock("@/pages/outline/OutlinePageSections", () => ({
  OutlineActionsBar: () => <div>outline-actions</div>,
  OutlineEditorSection: (props: { content: string }) => <div>outline-editor:{props.content}</div>,
  OutlineGenerationModal: () => null,
  OutlineGuideSection: () => <div>outline-guide</div>,
  OutlineHeaderSection: () => <div>outline-header</div>,
  OutlineParsingModal: () => null,
  OutlineTitleModal: () => null,
}));
vi.mock("@/pages/outline/DetailedOutlineSection", () => ({
  DetailedOutlineGenerationModal: () => null,
  DetailedOutlineSection: () => <div>detailed-outline</div>,
}));
vi.mock("@/components/atelier/WizardNextBar", () => ({ WizardNextBar: () => null }));
vi.mock("@/components/ui/GenerationFloatingCard", () => ({ GenerationFloatingCard: () => null }));
vi.mock("@/hooks/useUnsavedChangesGuard", () => ({ UnsavedChangesGuard: () => null }));

function makeState(overrides: Partial<OutlinePageState> = {}): OutlinePageState {
  return {
    loading: false,
    blockingLoadError: null,
    refreshLoadError: null,
    reload: vi.fn(async () => undefined),
    dirty: false,
    showUnsavedGuard: false,
    headerProps: {} as OutlinePageState["headerProps"],
    actionsBarProps: {} as OutlinePageState["actionsBarProps"],
    editorProps: { content: "已加载大纲", onChange: vi.fn() },
    titleModalProps: {} as OutlinePageState["titleModalProps"],
    generationModalProps: {} as OutlinePageState["generationModalProps"],
    parsingModalProps: {} as OutlinePageState["parsingModalProps"],
    wizardBarProps: {} as OutlinePageState["wizardBarProps"],
    detailedOutlineState: {
      items: [],
      generateModalOpen: false,
      generating: false,
    } as unknown as OutlinePageState["detailedOutlineState"],
    outlineGenFloatingProps: {} as OutlinePageState["outlineGenFloatingProps"],
    parsingFloatingProps: {} as OutlinePageState["parsingFloatingProps"],
    detailedGenFloatingProps: {} as OutlinePageState["detailedGenFloatingProps"],
    skeletonGenFloatingProps: {} as OutlinePageState["skeletonGenFloatingProps"],
    switchToDetailedRequested: false,
    clearSwitchToDetailedRequest: vi.fn(),
    ...overrides,
  };
}

describe("OutlinePage load errors", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("derives blocking and stale-refresh states without hiding loaded data", () => {
    const error = new ApiError({
      code: "OUTLINE_LOAD_FAILED",
      message: "大纲加载失败",
      requestId: "rid-outline",
      status: 503,
    });

    expect(deriveOutlinePageLoadState({ data: null, error, loading: false })).toEqual({
      loading: false,
      blockingLoadError: error,
      refreshLoadError: null,
    });
    expect(deriveOutlinePageLoadState({ data: { content: "stale" }, error, loading: true })).toEqual({
      loading: false,
      blockingLoadError: null,
      refreshLoadError: error,
    });
  });

  it("blocks the editor on initial failure and recovers after retry", async () => {
    const success = makeState();
    const reload = vi.fn(async () => {
      mocks.useOutlinePageState.mockReturnValue(success);
    });
    mocks.useOutlinePageState.mockReturnValue(
      makeState({
        blockingLoadError: new ApiError({
          code: "OUTLINE_LOAD_FAILED",
          message: "大纲加载失败",
          requestId: "rid-outline",
          status: 503,
        }),
        reload,
      }),
    );

    const view = render(<OutlinePage />);

    expect(screen.getByText("加载失败")).toBeInTheDocument();
    expect(screen.getByText("大纲加载失败 (OUTLINE_LOAD_FAILED)")).toBeInTheDocument();
    expect(screen.getByText("request_id: rid-outline")).toBeInTheDocument();
    expect(screen.queryByText("outline-header")).not.toBeInTheDocument();
    expect(screen.queryByText("outline-actions")).not.toBeInTheDocument();
    expect(screen.queryByText(/outline-editor:/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
    view.rerender(<OutlinePage />);

    expect(screen.queryByText("加载失败")).not.toBeInTheDocument();
    expect(screen.getByText("outline-header")).toBeInTheDocument();
    expect(screen.getByText("outline-editor:已加载大纲")).toBeInTheDocument();
  });

  it("keeps stale content visible on refresh failure and clears the warning after retry", async () => {
    const success = makeState();
    const reload = vi.fn(async () => {
      mocks.useOutlinePageState.mockReturnValue(success);
    });
    mocks.useOutlinePageState.mockReturnValue(
      makeState({
        refreshLoadError: new ApiError({
          code: "OUTLINE_REFRESH_FAILED",
          message: "刷新失败",
          requestId: "unknown",
          status: 503,
        }),
        reload,
      }),
    );

    const view = render(<OutlinePage />);

    expect(screen.queryByText("加载中...")).not.toBeInTheDocument();
    expect(screen.getByText("outline-editor:已加载大纲")).toBeInTheDocument();
    expect(screen.getByText("最近一次刷新失败")).toBeInTheDocument();
    expect(screen.getByText("刷新失败 (OUTLINE_REFRESH_FAILED)")).toBeInTheDocument();
    expect(screen.queryByText(/request_id:/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
    view.rerender(<OutlinePage />);

    expect(screen.queryByText("最近一次刷新失败")).not.toBeInTheDocument();
    expect(screen.getByText("outline-editor:已加载大纲")).toBeInTheDocument();
  });

  it("does not retry a stale refresh while the editor has unsaved changes", () => {
    const reload = vi.fn(async () => undefined);
    mocks.useOutlinePageState.mockReturnValue(
      makeState({
        dirty: true,
        refreshLoadError: new ApiError({
          code: "OUTLINE_REFRESH_FAILED",
          message: "刷新失败",
          requestId: "rid-dirty",
          status: 503,
        }),
        reload,
      }),
    );

    render(<OutlinePage />);

    expect(screen.getByText("outline-editor:已加载大纲")).toBeInTheDocument();
    expect(screen.getByText("请先保存或放弃未保存修改再重试")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
    expect(reload).not.toHaveBeenCalled();
  });
});
