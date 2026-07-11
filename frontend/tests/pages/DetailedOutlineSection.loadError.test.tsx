// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DetailedOutlineSection } from "@/pages/outline/DetailedOutlineSection";
import type { DetailedOutlineState } from "@/pages/outline/useDetailedOutlineState";
import { ApiError } from "@/services/apiClient";

function state(overrides: Partial<DetailedOutlineState> = {}): DetailedOutlineState {
  return {
    items: [],
    loading: false,
    error: null,
    hasData: true,
    hasLoaded: true,
    selected: null,
    selectedId: null,
    detailLoading: false,
    detailError: null,
    detailHasData: false,
    detailHasLoaded: false,
    generating: false,
    progress: null,
    skeletonGenerating: false,
    skeletonProgress: null,
    skeletonStreamRawText: "",
    skeletonStreamResult: null,
    editing: false,
    editContent: "",
    editTitle: "",
    saving: false,
    generateModalOpen: false,
    skeletonModalOpen: false,
    refresh: vi.fn(async () => undefined),
    reload: vi.fn(async () => undefined),
    reset: vi.fn(),
    selectVolume: vi.fn(async () => undefined),
    reloadDetail: vi.fn(async () => undefined),
    resetDetail: vi.fn(),
    deselectVolume: vi.fn(),
    openGenerateModal: vi.fn(),
    closeGenerateModal: vi.fn(),
    generate: vi.fn(async () => false),
    cancelGenerate: vi.fn(),
    openSkeletonModal: vi.fn(),
    closeSkeletonModal: vi.fn(),
    cancelSkeletonGenerate: vi.fn(),
    generateChapterSkeleton: vi.fn(async () => undefined),
    startEdit: vi.fn(),
    cancelEdit: vi.fn(),
    setEditContent: vi.fn(),
    setEditTitle: vi.fn(),
    saveEdit: vi.fn(async () => undefined),
    deleteVolume: vi.fn(async () => undefined),
    createChapters: vi.fn(async () => undefined),
    ...overrides,
  };
}

const listItem = {
  id: "detail-1",
  outline_id: "outline-1",
  volume_number: 1,
  volume_title: "第一卷",
  status: "planned" as const,
  chapter_count: 0,
  updated_at: "2026-07-12T00:00:00Z",
};

const selected = {
  ...listItem,
  project_id: "project-1",
  content_md: "细纲内容",
  structure: null,
  created_at: "2026-07-12T00:00:00Z",
};

describe("DetailedOutlineSection load errors", () => {
  it("shows a blocking initial error without an empty state or generation action", () => {
    const reload = vi.fn(async () => undefined);
    render(
      <DetailedOutlineSection
        {...state({
          hasData: false,
          error: new ApiError({ code: "LIST_FAILED", message: "列表失败", requestId: "rid-list", status: 503 }),
          reload,
        })}
      />,
    );

    expect(screen.getByText("细纲列表加载失败")).toBeInTheDocument();
    expect(screen.queryByText("暂无细纲")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /生成细纲/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("keeps stale list items visible under a refresh warning", () => {
    render(
      <DetailedOutlineSection
        {...state({
          items: [listItem],
          error: new ApiError({
            code: "LIST_REFRESH_FAILED",
            message: "刷新失败",
            requestId: "rid-list-refresh",
            status: 503,
          }),
        })}
      />,
    );

    expect(screen.getByText("细纲列表刷新失败")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /第一卷/ })).toBeInTheDocument();
  });

  it("keeps the list selection and offers an inline detail retry", () => {
    const reloadDetail = vi.fn(async () => undefined);
    render(
      <DetailedOutlineSection
        {...state({
          items: [listItem],
          selectedId: "detail-1",
          detailError: new ApiError({
            code: "DETAIL_FAILED",
            message: "详情失败",
            requestId: "rid-detail",
            status: 503,
          }),
          detailHasLoaded: true,
          reloadDetail,
        })}
      />,
    );

    expect(screen.getByRole("button", { name: /第一卷/ })).toBeInTheDocument();
    expect(screen.getByText("细纲详情加载失败")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(reloadDetail).toHaveBeenCalledTimes(1);
  });

  it("does not allow a stale detail retry to overwrite an active edit", () => {
    const reloadDetail = vi.fn(async () => undefined);
    render(
      <DetailedOutlineSection
        {...state({
          items: [listItem],
          selected,
          selectedId: "detail-1",
          detailError: new ApiError({
            code: "DETAIL_REFRESH_FAILED",
            message: "详情刷新失败",
            requestId: "rid-detail-refresh",
            status: 503,
          }),
          detailHasData: true,
          detailHasLoaded: true,
          editing: true,
          editTitle: "编辑中的标题",
          editContent: "编辑中的内容",
          reloadDetail,
        })}
      />,
    );

    expect(screen.getByText("细纲详情刷新失败")).toBeInTheDocument();
    expect(screen.getByText("请先保存或取消当前编辑，再重试加载详情")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
    expect(reloadDetail).not.toHaveBeenCalled();
  });

  it("disables switching to another volume while the current volume is being edited", () => {
    const selectVolume = vi.fn(async () => undefined);
    render(
      <DetailedOutlineSection
        {...state({
          items: [listItem, { ...listItem, id: "detail-2", volume_number: 2, volume_title: "第二卷" }],
          selected,
          selectedId: "detail-1",
          editing: true,
          editTitle: "编辑中的标题",
          editContent: "未保存的内容",
          selectVolume,
        })}
      />,
    );

    const otherVolume = screen.getByRole("button", { name: /第二卷/ });
    expect(otherVolume).toBeDisabled();
    expect(otherVolume).toHaveAttribute("title", "请先保存或取消当前编辑，再切换细纲");
    fireEvent.click(otherVolume);
    expect(selectVolume).not.toHaveBeenCalled();
  });
});
