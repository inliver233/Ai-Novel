// @vitest-environment jsdom
import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PreviewPage } from "@/pages/PreviewPage";
import { ApiError } from "@/services/apiClient";

const mocks = vi.hoisted(() => ({
  reload: vi.fn(async () => undefined),
  error: null as ApiError | null,
  capturedWizardBarProps: null as Record<string, unknown> | null,
  chapterMetaOptions: null as Record<string, unknown> | null,
  chapterDetailOptions: null as Record<string, unknown> | null,
}));

vi.mock("@/hooks/useWizardProgress", () => ({
  useWizardProgress: () => ({
    bumpLocal: vi.fn(),
    error: mocks.error,
    loading: false,
    progress: { percent: 50, steps: [], nextStep: null, exportedAt: null },
    reload: mocks.reload,
  }),
}));
vi.mock("@/hooks/useChapterMetaList", () => ({
  useChapterMetaList: (_projectId: string, options: Record<string, unknown>) => {
    mocks.chapterMetaOptions = options;
    return {
      chapters: [],
      error: null,
      hasData: true,
      hasLoaded: true,
      loading: false,
      refresh: vi.fn(async () => []),
      stale: false,
    };
  },
}));
vi.mock("@/hooks/useChapterDetail", () => ({
  useChapterDetail: (_chapterId: string | null, options: Record<string, unknown>) => {
    mocks.chapterDetailOptions = options;
    return {
      chapter: null,
      error: null,
      hasData: false,
      hasLoaded: false,
      loading: false,
      refresh: vi.fn(async () => null),
      stale: false,
    };
  },
}));
vi.mock("react-router-dom", () => ({
  useParams: () => ({ projectId: "project-1" }),
  useNavigate: () => vi.fn(),
}));
vi.mock("@/hooks/useIsMobile", () => ({ useIsMobile: () => false }));
vi.mock("@/services/chapterStore", () => ({
  chapterStore: { prefetchChapterDetail: vi.fn(async () => undefined) },
}));
vi.mock("@/services/wizard", () => ({ markWizardPreviewSeen: vi.fn() }));
vi.mock("@/components/layout/AppShell", () => ({
  PaperContent: (props: { children: React.ReactNode }) => <main>{props.children}</main>,
}));
vi.mock("@/components/writing/ChapterVirtualList", () => ({ ChapterVirtualList: () => <div>chapter-list</div> }));
vi.mock("@/components/ui/Drawer", () => ({ Drawer: (props: { children: React.ReactNode }) => props.children }));
vi.mock("@/components/atelier/WizardNextBar", () => ({
  WizardNextBar: (props: Record<string, unknown>) => {
    mocks.capturedWizardBarProps = props;
    return null;
  },
}));

describe("PreviewPage wizard error wiring", () => {
  it("passes useWizardProgress error and reload to WizardNextBar", () => {
    mocks.error = new ApiError({
      code: "WIZARD_REFRESH_FAILED",
      message: "向导刷新失败",
      requestId: "rid-preview-wizard",
      status: 503,
    });
    mocks.capturedWizardBarProps = null;

    render(<PreviewPage />);

    const captured = mocks.capturedWizardBarProps as Record<string, unknown> | null;
    expect(captured?.loadError).toBe(mocks.error);
    expect(captured?.onRetryLoad).toBe(mocks.reload);
    expect(mocks.chapterMetaOptions).toMatchObject({ toastOnError: false });
    expect(mocks.chapterDetailOptions).toMatchObject({ toastOnError: false });
  });
});
