// @vitest-environment jsdom
// M39 known_issue：useProjectData 加载失败时只 toast、不暴露 error 状态。
// frontend/src/hooks/useProjectData.ts:7-12,44-50：
//   - 返回类型 ProjectDataResult = { data, setData, loading, refresh }，无 error 字段；
//   - refresh 的 catch 分支仅调用 toast.toastError(...)，data 保持 null、loading 归 false，
//     但错误信息无处暴露。
// 后果：消费方无法可编程地感知加载错误，只能依赖 toast（瞬态、不可轮询），
// 因而无法渲染重试 UI（retry button）/错误占位（error fallback）。
// 本测试断言【正确行为】：加载失败后 hook 结果应暴露 error 状态（如 result.current.error
// 为 truthy 或 status==='error'），使消费方可编程感知错误。当前实现未暴露 → 断言 FAILED(红)。
import { describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

// vi.mock 工厂会被提升到文件顶部执行，故用 vi.hoisted 声明桩，避免 TDZ（参考
// ThemeToggle.test.tsx / AdminUsersPage.refresh known_issue 测试）。toast 必须返回稳定引用：
// refresh 的 useCallback deps 含 toast，若每次 render 返回新对象 → 回调身份变化 →
// 挂载 effect 每 render 重跑 → 无限循环（见 ImportPage stall 测试教训）。
const mocks = vi.hoisted(() => ({
  toast: {
    toastSuccess: vi.fn(),
    toastWarning: vi.fn(),
    toastError: vi.fn(),
  } as const,
}));

// useProjectData 仅用到 ApiError（用于 instanceof 判定）。提供最小 ApiError 类即可，
// 使测试中抛出的 ApiError 实例与 hook 内 instanceof 判定指向同一（mock）类。
vi.mock("@/services/apiClient", () => ({
  ApiError: class ApiError extends Error {
    code: string;
    requestId: string;
    status: number;
    constructor(args: { code: string; message: string; requestId: string; status: number }) {
      super(args.message);
      this.name = "ApiError";
      this.code = args.code;
      this.requestId = args.requestId;
      this.status = args.status;
    }
  },
}));

// useToast 需 ToastProvider；用 no-op mock 隔离，避免拉入 portal/全局副作用。
vi.mock("@/components/ui/toast", () => ({
  useToast: () => mocks.toast,
}));

import { useProjectData, type ProjectDataResult } from "@/hooks/useProjectData";
import { ApiError } from "@/services/apiClient";

describe("useProjectData 加载失败应暴露 error (M39 known_issue)", () => {
  it("loader 抛 ApiError 后，hook 结果应暴露 error 状态", { tags: ["@known_issue"] }, async () => {
    // M39：loader 抛 ApiError（与 hook 内 instanceof 判定同源），模拟后端 500。
    const apiError = new ApiError({
      code: "INTERNAL_ERROR",
      message: "服务器内部错误",
      requestId: "rid-m39",
      status: 500,
    });
    const failingLoader = vi.fn().mockRejectedValue(apiError);

    const { result } = renderHook(() => useProjectData("proj-m39", failingLoader));

    // 前置确认：错误路径确已触发——toast.toastError 被调用（证明 loader 抛错、catch 命中）。
    // 若此处失败说明测试搭建错误而非 bug。
    await waitFor(() => {
      expect(mocks.toast.toastError).toHaveBeenCalled();
    });

    // 前置确认：加载已 settle（loading 归 false），排除"尚未到达错误终态"的歧义。
    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    // M39 正确行为断言：hook 结果应暴露 error 状态，使消费方可编程感知错误并渲染重试 UI。
    // 当前 ProjectDataResult 类型未声明 error，断言前以期望形态做类型收窄（known_issue 诚实镜像）。
    const exposed = result.current as ProjectDataResult<unknown> & { error?: unknown; status?: string };
    expect(exposed.error).toBeTruthy();
  });
});
