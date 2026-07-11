// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { WritingEditorSection, type WritingEditorSectionProps } from "@/pages/writing/WritingPageSections";
import { ApiError } from "@/services/apiClient";

vi.mock("@/components/atelier/MarkdownEditor", () => ({
  MarkdownEditor: (props: { value: string }) => <div>chapter-content:{props.value}</div>,
}));

function makeProps(overrides: Partial<WritingEditorSectionProps> = {}): WritingEditorSectionProps {
  return {
    activeChapter: null,
    form: null,
    dirty: false,
    isDoneReadonly: false,
    loadingChapter: false,
    loadError: null,
    generating: false,
    saving: false,
    autoUpdatesTriggering: false,
    contentEditorTab: "edit",
    onContentEditorTabChange: vi.fn(),
    onTitleChange: vi.fn(),
    onStatusChange: vi.fn(),
    onPlanChange: vi.fn(),
    onContentChange: vi.fn(),
    onSummaryChange: vi.fn(),
    onDeleteChapter: vi.fn(),
    onRetryLoad: vi.fn(),
    onSaveAndTriggerAutoUpdates: vi.fn(),
    onSaveChapter: vi.fn(),
    onReopenDrafting: vi.fn(),
    ...overrides,
  };
}

describe("WritingEditorSection detail load errors", () => {
  it("shows an inline retry instead of the normal empty state and keeps the list area outside the editor untouched", () => {
    const onRetryLoad = vi.fn();
    const failedProps = makeProps({
      loadError: new ApiError({
        code: "CHAPTER_DETAIL_FAILED",
        message: "章节正文加载失败",
        requestId: "rid-detail",
        status: 503,
      }),
      onRetryLoad,
    });
    const view = render(<WritingEditorSection {...failedProps} />);

    expect(screen.getByText("章节加载失败")).toBeInTheDocument();
    expect(screen.getByText("章节正文加载失败 (CHAPTER_DETAIL_FAILED)")).toBeInTheDocument();
    expect(screen.queryByText("请选择或新建章节开始写作。")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(onRetryLoad).toHaveBeenCalledTimes(1);

    view.rerender(
      <WritingEditorSection
        {...makeProps({
          activeChapter: {
            id: "chapter-1",
            project_id: "project-1",
            outline_id: "outline-1",
            number: 1,
            title: "第一章",
            plan: "计划",
            content_md: "正文",
            summary: "摘要",
            status: "drafting",
            updated_at: "2026-07-12T00:00:00Z",
          },
          form: { title: "第一章", plan: "计划", content_md: "正文", summary: "摘要", status: "drafting" },
        })}
      />,
    );

    expect(screen.queryByText("章节加载失败")).not.toBeInTheDocument();
    expect(screen.getByText("chapter-content:正文")).toBeInTheDocument();
  });
});
