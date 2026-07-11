// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectWizardPage } from "@/pages/ProjectWizardPage";
import { ApiError } from "@/services/apiClient";

const mocks = vi.hoisted(() => ({
  navigate: vi.fn(),
  toast: { toastSuccess: vi.fn(), toastWarning: vi.fn(), toastError: vi.fn() },
  useProjectData: vi.fn(),
  wizardQuery: {
    data: null as null | {
      settings: Record<string, unknown>;
      characters: never[];
      outline: { id: string; content_md: string; structure: null };
      llmPreset: Record<string, unknown>;
      profiles: never[];
    },
    error: null as ApiError | null,
    loading: false,
    refresh: vi.fn(async () => undefined),
    resetError: vi.fn(),
  },
  chapterQuery: {
    chapters: [],
    error: null as ApiError | null,
    hasData: true,
    hasLoaded: true,
    loading: false,
    refresh: vi.fn(async () => []),
    stale: false,
  },
  capturedWizardBarProps: null as Record<string, unknown> | null,
}));

vi.mock("@/hooks/useProjectData", () => ({ useProjectData: mocks.useProjectData }));
vi.mock("@/hooks/useChapterMetaList", () => ({ useChapterMetaList: () => mocks.chapterQuery }));
vi.mock("react-router-dom", () => ({
  useParams: () => ({ projectId: "project-1" }),
  useNavigate: () => mocks.navigate,
}));
vi.mock("framer-motion", () => ({
  motion: { div: (props: React.HTMLAttributes<HTMLDivElement>) => <div {...props} /> },
  useReducedMotion: () => true,
}));
vi.mock("@/components/ui/toast", () => ({ useToast: () => mocks.toast }));
vi.mock("@/components/ui/confirm", () => ({
  useConfirm: () => ({ confirm: vi.fn(async () => true), choose: vi.fn(async () => "cancel") }),
}));
vi.mock("@/contexts/projects", () => ({
  useProjects: () => ({ projects: [{ id: "project-1", name: "测试项目", llm_profile_id: null }] }),
}));
vi.mock("@/components/atelier/WizardNextBar", () => ({
  WizardNextBar: (props: Record<string, unknown>) => {
    mocks.capturedWizardBarProps = props;
    return null;
  },
}));

function loadedData() {
  return {
    settings: {},
    characters: [] as never[],
    outline: { id: "outline-1", content_md: "# 大纲", structure: null },
    llmPreset: {},
    profiles: [] as never[],
  };
}

describe("ProjectWizardPage load errors", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.wizardQuery.data = null;
    mocks.wizardQuery.error = null;
    mocks.wizardQuery.loading = false;
    mocks.wizardQuery.refresh.mockResolvedValue(undefined);
    mocks.chapterQuery.error = null;
    mocks.chapterQuery.hasData = true;
    mocks.chapterQuery.loading = false;
    mocks.chapterQuery.refresh.mockResolvedValue([]);
    mocks.capturedWizardBarProps = null;
    mocks.useProjectData.mockReturnValue(mocks.wizardQuery);
  });

  it("blocks false 0% progress on initial failure and recovers after retry", async () => {
    mocks.wizardQuery.error = new ApiError({
      code: "WIZARD_LOAD_FAILED",
      message: "向导数据加载失败",
      requestId: "rid-wizard-page",
      status: 503,
    });
    mocks.wizardQuery.refresh.mockImplementationOnce(async () => {
      mocks.wizardQuery.data = loadedData();
      mocks.wizardQuery.error = null;
    });

    const view = render(<ProjectWizardPage />);

    expect(screen.getByText("向导数据加载失败 (WIZARD_LOAD_FAILED)")).toBeInTheDocument();
    expect(screen.queryByText(/完成度：0%/)).not.toBeInTheDocument();
    expect(screen.queryByText("待完成")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(mocks.wizardQuery.refresh).toHaveBeenCalledTimes(1));
    view.rerender(<ProjectWizardPage />);
    expect(screen.queryByText("向导数据加载失败 (WIZARD_LOAD_FAILED)")).not.toBeInTheDocument();
    expect(screen.getByText(/完成度：/)).toBeInTheDocument();
    expect(mocks.toast.toastError).not.toHaveBeenCalled();
  });

  it("keeps loaded progress visible and wires stale error retry into the bottom bar", async () => {
    mocks.wizardQuery.data = loadedData();
    mocks.wizardQuery.error = new ApiError({
      code: "WIZARD_REFRESH_FAILED",
      message: "向导刷新失败",
      requestId: "unknown",
      status: 503,
    });
    mocks.chapterQuery.error = new ApiError({
      code: "CHAPTER_PROGRESS_FAILED",
      message: "章节进度刷新失败",
      requestId: "rid-chapter-progress",
      status: 503,
    });

    render(<ProjectWizardPage />);

    expect(screen.getByText(/完成度：/)).toBeInTheDocument();
    expect(screen.getByText("最近一次刷新失败")).toBeInTheDocument();
    expect(screen.getByText("最近一次章节进度刷新失败")).toBeInTheDocument();
    expect(mocks.capturedWizardBarProps?.loadError).toBe(mocks.wizardQuery.error);
    await (mocks.capturedWizardBarProps?.onRetryLoad as () => Promise<void>)();
    expect(mocks.wizardQuery.refresh).toHaveBeenCalledTimes(1);
    expect(mocks.chapterQuery.refresh).toHaveBeenCalledTimes(1);
    expect(mocks.toast.toastError).not.toHaveBeenCalled();
  });
});
