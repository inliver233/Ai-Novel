// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WritingPage } from "@/pages/WritingPage";
import type { WritingPageState } from "@/pages/writing/useWritingPageState";
import { ApiError } from "@/services/apiClient";

const mocks = vi.hoisted(() => ({ useWritingPageState: vi.fn() }));

vi.mock("@/pages/writing/useWritingPageState", () => ({ useWritingPageState: mocks.useWritingPageState }));
vi.mock("@/components/layout/AppShell", () => ({
  ToolContent: (props: { children: React.ReactNode }) => <main>{props.children}</main>,
}));
vi.mock("@/pages/writing/WritingPageSections", () => ({
  WritingChapterListDrawer: () => <div>chapter-drawer</div>,
  WritingPageOverlays: () => <div>writing-overlays</div>,
  WritingStreamFloatingCard: () => <div>stream-card</div>,
  WritingWorkspace: () => <div>writing-workspace</div>,
}));
vi.mock("@/components/atelier/WizardNextBar", () => ({ WizardNextBar: () => null }));
vi.mock("@/hooks/useUnsavedChangesGuard", () => ({ UnsavedChangesGuard: () => null }));

function makeState(overrides: Partial<WritingPageState> = {}): WritingPageState {
  return {
    loading: false,
    metadataBlockingLoadError: null,
    metadataRefreshLoadError: null,
    reloadMetadata: vi.fn(async () => undefined),
    chapterListBlockingLoadError: null,
    chapterListRefreshLoadError: null,
    reloadChapterList: vi.fn(async () => undefined),
    dirty: false,
    showUnsavedGuard: false,
    workspaceProps: {} as WritingPageState["workspaceProps"],
    chapterListDrawerProps: {} as WritingPageState["chapterListDrawerProps"],
    overlaysProps: {} as WritingPageState["overlaysProps"],
    streamFloatingProps: {} as WritingPageState["streamFloatingProps"],
    wizardBarProps: {} as WritingPageState["wizardBarProps"],
    ...overrides,
  };
}

function loadError(code: string, requestId = "rid-writing") {
  return new ApiError({ code, message: "写作页加载失败", requestId, status: 503 });
}

describe("WritingPage load errors", () => {
  beforeEach(() => vi.clearAllMocks());

  it("does not render the workspace before initial metadata and chapter-list loading completes", () => {
    mocks.useWritingPageState.mockReturnValue(makeState({ loading: true }));

    render(<WritingPage />);

    expect(screen.getByText("加载中...")).toBeInTheDocument();
    expect(screen.queryByText("writing-workspace")).not.toBeInTheDocument();
  });

  it("blocks the workspace on metadata failure and recovers after retry", async () => {
    const success = makeState();
    const reloadMetadata = vi.fn(async () => {
      mocks.useWritingPageState.mockReturnValue(success);
    });
    mocks.useWritingPageState.mockReturnValue(
      makeState({ metadataBlockingLoadError: loadError("WRITING_LOAD_FAILED"), reloadMetadata }),
    );

    const view = render(<WritingPage />);

    expect(screen.getByText("写作页加载失败 (WRITING_LOAD_FAILED)")).toBeInTheDocument();
    expect(screen.queryByText("writing-workspace")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(reloadMetadata).toHaveBeenCalledTimes(1));
    view.rerender(<WritingPage />);
    expect(screen.getByText("writing-workspace")).toBeInTheDocument();
  });

  it("does not turn an initial chapter-list failure into the normal empty workspace", async () => {
    const success = makeState();
    const reloadChapterList = vi.fn(async () => {
      mocks.useWritingPageState.mockReturnValue(success);
    });
    mocks.useWritingPageState.mockReturnValue(
      makeState({
        chapterListBlockingLoadError: loadError("CHAPTER_LIST_FAILED", "rid-chapters"),
        reloadChapterList,
      }),
    );

    const view = render(<WritingPage />);

    expect(screen.getByText("章节列表加载失败")).toBeInTheDocument();
    expect(screen.queryByText("writing-workspace")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(reloadChapterList).toHaveBeenCalledTimes(1));
    view.rerender(<WritingPage />);
    expect(screen.getByText("writing-workspace")).toBeInTheDocument();
  });

  it("keeps stale workspace content visible and blocks metadata retry while dirty", () => {
    const reloadMetadata = vi.fn(async () => undefined);
    mocks.useWritingPageState.mockReturnValue(
      makeState({
        dirty: true,
        metadataRefreshLoadError: loadError("WRITING_REFRESH_FAILED", "unknown"),
        reloadMetadata,
      }),
    );

    render(<WritingPage />);

    expect(screen.getByText("writing-workspace")).toBeInTheDocument();
    expect(screen.getByText("最近一次刷新失败")).toBeInTheDocument();
    expect(screen.getByText("请先保存或放弃未保存修改再重试")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
    expect(reloadMetadata).not.toHaveBeenCalled();
  });

  it("shows chapter-list refresh failure separately and retries without hiding the workspace", async () => {
    const success = makeState();
    const reloadChapterList = vi.fn(async () => {
      mocks.useWritingPageState.mockReturnValue(success);
    });
    mocks.useWritingPageState.mockReturnValue(
      makeState({
        chapterListRefreshLoadError: loadError("CHAPTER_LIST_REFRESH_FAILED", "rid-list-refresh"),
        reloadChapterList,
      }),
    );

    const view = render(<WritingPage />);

    expect(screen.getByText("writing-workspace")).toBeInTheDocument();
    expect(screen.getByText("最近一次章节列表刷新失败")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(reloadChapterList).toHaveBeenCalledTimes(1));
    view.rerender(<WritingPage />);
    expect(screen.queryByText("最近一次章节列表刷新失败")).not.toBeInTheDocument();
    expect(screen.getByText("writing-workspace")).toBeInTheDocument();
  });

  it("blocks chapter-list refresh retry while dirty", () => {
    const reloadChapterList = vi.fn(async () => undefined);
    mocks.useWritingPageState.mockReturnValue(
      makeState({
        dirty: true,
        chapterListRefreshLoadError: loadError("CHAPTER_LIST_REFRESH_FAILED"),
        reloadChapterList,
      }),
    );

    render(<WritingPage />);

    expect(screen.getByText("writing-workspace")).toBeInTheDocument();
    expect(screen.getByText("最近一次章节列表刷新失败")).toBeInTheDocument();
    expect(screen.getByText("请先保存或放弃未保存修改再重试")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
    expect(reloadChapterList).not.toHaveBeenCalled();
  });
});
