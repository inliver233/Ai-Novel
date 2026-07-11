// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { WizardNextBar } from "@/components/atelier/WizardNextBar";
import { ApiError } from "@/services/apiClient";
import type { WizardProgress } from "@/services/wizard";

vi.mock("react-router-dom", () => ({ useNavigate: () => vi.fn() }));

const progress = {
  percent: 50,
  nextStep: null,
  exportedAt: null,
  steps: [
    {
      key: "settings",
      title: "设置",
      description: "完成设置",
      href: "/settings",
      state: "done",
    },
  ],
} as WizardProgress;

describe("WizardNextBar load error", () => {
  it("shows a compact retry without exposing false navigation state", () => {
    const onRetryLoad = vi.fn();
    render(
      <WizardNextBar
        projectId="project-1"
        currentStep="settings"
        progress={progress}
        loadError={
          new ApiError({
            code: "WIZARD_REFRESH_FAILED",
            message: "向导刷新失败",
            requestId: "rid-wizard-bar",
            status: 503,
          })
        }
        onRetryLoad={onRetryLoad}
      />,
    );

    expect(screen.getByText(/向导进度刷新失败：向导刷新失败 \(WIZARD_REFRESH_FAILED\)/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(onRetryLoad).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: /向导加载失败/ })).toBeDisabled();
  });

  it("blocks retry while the owning page is dirty", () => {
    const onRetryLoad = vi.fn();
    render(
      <WizardNextBar
        projectId="project-1"
        currentStep="settings"
        progress={progress}
        dirty
        loadError={
          new ApiError({
            code: "WIZARD_REFRESH_FAILED",
            message: "向导刷新失败",
            requestId: "unknown",
            status: 503,
          })
        }
        onRetryLoad={onRetryLoad}
        retryBlockedReason="请先保存或放弃未保存修改再重试"
      />,
    );

    expect(screen.getByText("请先保存或放弃未保存修改再重试")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
    expect(onRetryLoad).not.toHaveBeenCalled();
  });
});
