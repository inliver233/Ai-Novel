// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CharactersPage } from "@/pages/CharactersPage";
import { ApiError } from "@/services/apiClient";
import type { Character } from "@/types";

const mocks = vi.hoisted(() => ({
  query: {
    data: null as Character[] | null,
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

function character(): Character {
  return {
    id: "character-1",
    project_id: "project-1",
    name: "林默",
    role: "主角",
    profile: "旧人物档案",
    notes: "旧备注",
    updated_at: "2026-07-12T00:00:00Z",
  };
}

function loadError(code = "CHARACTERS_REFRESH_FAILED") {
  return new ApiError({ code, message: "角色列表请求失败", requestId: "rid-characters", status: 503 });
}

describe("CharactersPage load errors", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.assign(mocks.query, {
      data: null,
      error: null,
      loading: false,
    });
  });

  it("shows a blocking initial error without a false empty state and retries", () => {
    Object.assign(mocks.query, { error: loadError("CHARACTERS_LOAD_FAILED"), data: null });

    render(<CharactersPage />);

    expect(screen.getByText("角色列表加载失败")).toBeInTheDocument();
    expect(screen.queryByText("暂无角色")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(mocks.query.refresh).toHaveBeenCalledTimes(1);
  });

  it("keeps stale character cards visible under a refresh warning", () => {
    Object.assign(mocks.query, { data: [character()], error: loadError() });

    render(<CharactersPage />);

    expect(screen.getByText("角色列表刷新失败")).toBeInTheDocument();
    expect(screen.getByText("林默")).toBeInTheDocument();
    expect(screen.getByText("request_id: rid-characters")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(mocks.query.refresh).toHaveBeenCalledTimes(1);
  });

  it("keeps the successful empty state visible with a stale refresh warning", () => {
    Object.assign(mocks.query, { data: [], error: loadError() });

    render(<CharactersPage />);

    expect(screen.getByText("角色列表刷新失败")).toBeInTheDocument();
    expect(screen.getByText("暂无角色")).toBeInTheDocument();
  });

  it("keeps the filtered-empty state visible with a stale refresh warning", () => {
    Object.assign(mocks.query, { data: [character()], error: loadError() });
    render(<CharactersPage />);

    fireEvent.change(screen.getByRole("textbox", { name: "角色搜索" }), { target: { value: "不存在" } });

    expect(screen.getByText("角色列表刷新失败")).toBeInTheDocument();
    expect(screen.getByText("没有匹配的角色")).toBeInTheDocument();
  });

  it("allows stale retry without overwriting dirty drawer form state", () => {
    Object.assign(mocks.query, { data: [character()], error: loadError() });
    render(<CharactersPage />);

    fireEvent.click(screen.getByText("林默"));
    const profile = screen.getByPlaceholderText("外貌、性格、动机、关系、口癖、成长线…");
    fireEvent.change(profile, { target: { value: "本地未保存人物档案" } });
    expect(screen.getByText("未保存")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));

    expect(mocks.query.refresh).toHaveBeenCalledTimes(1);
    expect(screen.getByPlaceholderText("外貌、性格、动机、关系、口癖、成长线…")).toHaveValue("本地未保存人物档案");
    expect(screen.getByText("未保存")).toBeInTheDocument();
  });
});
