// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SettingsPage } from "@/pages/SettingsPage";
import { ApiError } from "@/services/apiClient";
import type { SettingsLoaded } from "@/pages/settings/models";

const mocks = vi.hoisted(() => ({
  query: {
    data: null as SettingsLoaded | null,
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
vi.mock("@/hooks/useSaveHotkey", () => ({ useSaveHotkey: () => undefined }));
vi.mock("@/hooks/usePersistentOutlet", () => ({ usePersistentOutletIsActive: () => true }));
vi.mock("@/hooks/useUnsavedChangesGuard", () => ({ UnsavedChangesGuard: () => null }));
vi.mock("@/components/ui/toast", () => ({ useToast: () => mocks.toast }));
vi.mock("@/components/atelier/WizardNextBar", () => ({ WizardNextBar: () => null }));
vi.mock("@/contexts/auth", () => ({ useAuth: () => ({ user: { id: "viewer-1" } }) }));
vi.mock("@/contexts/projects", () => ({ useProjects: () => ({ refresh: vi.fn() }) }));
vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
  useParams: () => ({ projectId: "project-1" }),
}));
vi.mock("@/services/wizard", () => ({ markWizardProjectChanged: vi.fn() }));

function settingsLoaded(): SettingsLoaded {
  return {
    project: {
      id: "project-1",
      owner_user_id: "owner-1",
      name: "服务端项目名",
      genre: "科幻",
      logline: "一句话梗概",
      created_at: "2026-07-12T00:00:00Z",
      updated_at: "2026-07-12T00:00:00Z",
    },
    settings: {
      project_id: "project-1",
      world_setting: "服务端世界观",
      style_guide: "服务端风格",
      constraints: "服务端约束",
    },
  } as unknown as SettingsLoaded;
}

function loadError(code = "SETTINGS_REFRESH_FAILED") {
  return new ApiError({ code, message: "设置请求失败", requestId: "rid-settings", status: 503 });
}

describe("SettingsPage load errors", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.assign(mocks.query, {
      data: null,
      error: null,
      loading: false,
    });
  });

  it("shows a blocking initial error and retries", () => {
    Object.assign(mocks.query, { error: loadError("SETTINGS_LOAD_FAILED"), data: null });

    render(<SettingsPage />);

    expect(screen.getByText("加载失败")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(mocks.query.refresh).toHaveBeenCalledTimes(1);
  });

  it("keeps the stale form visible under a refresh warning instead of a skeleton", () => {
    Object.assign(mocks.query, { data: settingsLoaded(), error: loadError(), loading: true });

    render(<SettingsPage />);

    expect(screen.getByText("最近一次刷新失败")).toBeInTheDocument();
    expect(screen.getByText("request_id: rid-settings")).toBeInTheDocument();
    const nameInput = document.querySelector<HTMLInputElement>('input[name="project_name"]');
    expect(nameInput).not.toBeNull();
    expect(nameInput).toHaveValue("服务端项目名");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(mocks.query.refresh).toHaveBeenCalledTimes(1);
  });

  it("blocks stale retry while the form is dirty", () => {
    Object.assign(mocks.query, { data: settingsLoaded(), error: loadError() });

    render(<SettingsPage />);

    const nameInput = document.querySelector<HTMLInputElement>('input[name="project_name"]');
    expect(nameInput).not.toBeNull();
    fireEvent.change(nameInput!, { target: { value: "本地未保存项目名" } });

    expect(screen.getByText("最近一次刷新失败")).toBeInTheDocument();
    expect(screen.getByText("请先保存或放弃未保存修改再重试")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
    expect(mocks.query.refresh).not.toHaveBeenCalled();
  });

  it("does not overwrite a dirty form when a refetch completes", () => {
    Object.assign(mocks.query, { data: settingsLoaded(), error: null });

    const view = render(<SettingsPage />);

    const nameInput = document.querySelector<HTMLInputElement>('input[name="project_name"]');
    expect(nameInput).not.toBeNull();
    fireEvent.change(nameInput!, { target: { value: "本地未保存项目名" } });
    expect(nameInput).toHaveValue("本地未保存项目名");

    // 刷新成功：新对象身份的服务端数据到达，不得覆盖未保存表单。
    Object.assign(mocks.query, { data: settingsLoaded() });
    view.rerender(<SettingsPage />);

    expect(document.querySelector<HTMLInputElement>('input[name="project_name"]')).toHaveValue("本地未保存项目名");
  });
});
