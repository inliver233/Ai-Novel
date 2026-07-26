// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EntriesPage } from "@/pages/EntriesPage";
import { ApiError } from "@/services/apiClient";
import type { Entry } from "@/types";

const mocks = vi.hoisted(() => ({
  query: {
    data: null as Entry[] | null,
    error: null as ApiError | null,
    loading: false,
    refresh: vi.fn(async () => undefined),
    resetError: vi.fn(),
    setData: vi.fn(),
  },
  toast: {
    toastError: vi.fn(),
    toastSuccess: vi.fn(),
  },
  confirm: {
    confirm: vi.fn(async () => true),
  },
}));

vi.mock("@/hooks/useProjectData", () => ({ useProjectData: () => mocks.query }));
vi.mock("@/hooks/useWizardProgress", () => ({
  useWizardProgress: () => ({
    bumpLocal: vi.fn(),
    error: null,
    loading: false,
    progress: { percent: 50, steps: [], nextStep: null, exportedAt: null },
    refresh: vi.fn(async () => undefined),
    reload: vi.fn(async () => undefined),
  }),
}));
vi.mock("@/hooks/useAutoSave", () => ({ useAutoSave: () => ({ cancel: vi.fn(), flush: vi.fn() }) }));
vi.mock("@/hooks/useQueuedSave", () => ({
  useQueuedSave: () => ({ save: vi.fn(async () => true), saving: false }),
}));
vi.mock("@/hooks/useIsMobile", () => ({ useIsMobile: () => false }));
vi.mock("@/components/ui/toast", () => ({ useToast: () => mocks.toast }));
vi.mock("@/components/ui/confirm", () => ({ useConfirm: () => mocks.confirm }));
vi.mock("@/components/ui/Drawer", () => ({
  Drawer: (props: { children: React.ReactNode; open: boolean }) =>
    props.open ? <aside>{props.children}</aside> : null,
}));
vi.mock("@/components/atelier/WizardNextBar", () => ({ WizardNextBar: () => null }));
vi.mock("react-router-dom", () => ({ useParams: () => ({ projectId: "project-1" }) }));
vi.mock("@/services/wizard", () => ({ markWizardProjectChanged: vi.fn() }));
vi.mock("@/services/entriesApi", () => ({
  createEntry: vi.fn(),
  deleteEntry: vi.fn(),
  listEntries: vi.fn(async () => ({ items: [], next_offset: null })),
  updateEntry: vi.fn(),
}));

/** jsdom 不实现 matchMedia，polyfill 供 useReducedMotion / useIsMobile 使用。 */
function installMatchMediaPolyfill() {
  if (!window.matchMedia) {
    const impl = (query: string): MediaQueryList =>
      ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList;
    Object.defineProperty(window, "matchMedia", { configurable: true, writable: true, value: impl });
  }
}

function entry(): Entry {
  return {
    id: "entry-1",
    project_id: "project-1",
    title: "雨夜相遇",
    content: "黑伞男首次出现，留下未解线索",
    tags: ["设定"],
    created_at: "2026-07-12T00:00:00Z",
    updated_at: "2026-07-12T00:00:00Z",
  };
}

function loadError(code = "ENTRIES_REFRESH_FAILED") {
  return new ApiError({ code, message: "条目列表请求失败", requestId: "rid-entries", status: 503 });
}

describe("EntriesPage load errors", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installMatchMediaPolyfill();
    Object.assign(mocks.query, {
      data: null,
      error: null,
      loading: false,
    });
  });

  it("shows a blocking initial error without a false empty state and retries", () => {
    Object.assign(mocks.query, { error: loadError("ENTRIES_LOAD_FAILED"), data: null });

    render(<EntriesPage />);

    expect(screen.getByText("条目列表加载失败")).toBeInTheDocument();
    expect(screen.queryByText("暂无条目")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(mocks.query.refresh).toHaveBeenCalledTimes(1);
  });

  it("keeps stale entry cards visible under a refresh warning", () => {
    Object.assign(mocks.query, { data: [entry()], error: loadError() });

    render(<EntriesPage />);

    expect(screen.getByText("条目列表刷新失败")).toBeInTheDocument();
    expect(screen.getByText("雨夜相遇")).toBeInTheDocument();
    expect(screen.getByText("request_id: rid-entries")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(mocks.query.refresh).toHaveBeenCalledTimes(1);
  });

  it("keeps the successful empty state visible with a stale refresh warning", () => {
    Object.assign(mocks.query, { data: [], error: loadError() });

    render(<EntriesPage />);

    expect(screen.getByText("条目列表刷新失败")).toBeInTheDocument();
    expect(screen.getByText("暂无条目")).toBeInTheDocument();
  });

  it("keeps the filtered-empty state visible with a stale refresh warning", () => {
    Object.assign(mocks.query, { data: [entry()], error: loadError() });
    render(<EntriesPage />);

    fireEvent.change(screen.getByRole("textbox", { name: "条目搜索" }), { target: { value: "不存在" } });

    expect(screen.getByText("条目列表刷新失败")).toBeInTheDocument();
    expect(screen.getByText("没有匹配的条目")).toBeInTheDocument();
  });

  it("allows stale retry without overwriting dirty drawer form state", () => {
    Object.assign(mocks.query, { data: [entry()], error: loadError() });
    render(<EntriesPage />);

    fireEvent.click(screen.getByText("雨夜相遇"));
    const content = screen.getByPlaceholderText("记录设定细节、伏笔安排、情节想法、信息来源或后续待验证点…");
    fireEvent.change(content, { target: { value: "本地未保存条目内容" } });
    expect(screen.getByText("未保存")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));

    expect(mocks.query.refresh).toHaveBeenCalledTimes(1);
    expect(screen.getByPlaceholderText("记录设定细节、伏笔安排、情节想法、信息来源或后续待验证点…")).toHaveValue(
      "本地未保存条目内容",
    );
    expect(screen.getByText("未保存")).toBeInTheDocument();
  });
});
