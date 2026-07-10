// @vitest-environment jsdom
// M39/frontend-arch#8 回归：useProjectData 必须暴露稳定 error/reset/retry 状态，
// 且并发或 project 切换后的过期请求不得覆盖最新状态。
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

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
    details?: unknown;
    constructor(args: { code: string; message: string; requestId: string; status: number; details?: unknown }) {
      super(args.message);
      this.name = "ApiError";
      this.code = args.code;
      this.requestId = args.requestId;
      this.status = args.status;
      this.details = args.details;
    }
  },
}));

// useToast 需 ToastProvider；用 no-op mock 隔离，避免拉入 portal/全局副作用。
vi.mock("@/components/ui/toast", () => ({
  useToast: () => mocks.toast,
}));

import { useProjectData } from "@/hooks/useProjectData";
import { ApiError } from "@/services/apiClient";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  mocks.toast.toastSuccess.mockClear();
  mocks.toast.toastWarning.mockClear();
  mocks.toast.toastError.mockClear();
});

describe("useProjectData error state", () => {
  it("保留 ApiError identity 与追踪字段，并允许 resetError 清除", async () => {
    const apiError = new ApiError({
      code: "INTERNAL_ERROR",
      message: "服务器内部错误",
      requestId: "rid-m39",
      status: 500,
    });
    const failingLoader = vi.fn().mockRejectedValue(apiError);

    const { result } = renderHook(() => useProjectData("proj-m39", failingLoader));

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(result.current.error).toBe(apiError);
    expect(result.current.error).toMatchObject({
      code: "INTERNAL_ERROR",
      requestId: "rid-m39",
      status: 500,
    });
    expect(mocks.toast.toastError).not.toHaveBeenCalled();

    act(() => result.current.resetError());
    expect(result.current.error).toBeNull();
  });

  it("把未知异常规整化为可编程 ApiError", async () => {
    const thrown = new TypeError("raw failure");
    const { result } = renderHook(() => useProjectData("proj-unknown", vi.fn().mockRejectedValue(thrown)));

    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.error).toBeInstanceOf(ApiError);
    expect(result.current.error).toMatchObject({
      code: "UNKNOWN_ERROR",
      message: "请求失败",
      requestId: "unknown",
      status: 0,
      details: thrown,
    });
    expect(mocks.toast.toastError).not.toHaveBeenCalled();
  });

  it("refresh 重试时清除旧错误并在成功后写入数据", async () => {
    const apiError = new ApiError({ code: "TEMPORARY", message: "暂时失败", requestId: "rid-1", status: 503 });
    const retry = deferred<{ value: string }>();
    const loader = vi.fn().mockRejectedValueOnce(apiError).mockReturnValueOnce(retry.promise);
    const { result } = renderHook(() => useProjectData("proj-retry", loader));

    await waitFor(() => expect(result.current.error).toBe(apiError));

    let refreshPromise!: Promise<void>;
    act(() => {
      refreshPromise = result.current.refresh();
    });

    expect(result.current.loading).toBe(true);
    expect(result.current.error).toBeNull();

    await act(async () => {
      retry.resolve({ value: "recovered" });
      await refreshPromise;
    });

    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
    expect(result.current.data).toEqual({ value: "recovered" });
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it("同项目刷新失败时保留已加载数据并暴露错误", async () => {
    const failure = deferred<string>();
    const apiError = new ApiError({
      code: "REFRESH_FAILED",
      message: "刷新失败",
      requestId: "rid-refresh",
      status: 500,
    });
    const loader = vi.fn().mockResolvedValueOnce("old-data").mockReturnValueOnce(failure.promise);
    const { result } = renderHook(() => useProjectData("proj-stale", loader));

    await waitFor(() => expect(result.current.data).toBe("old-data"));
    let refreshPromise!: Promise<void>;
    act(() => {
      refreshPromise = result.current.refresh();
    });
    expect(result.current).toMatchObject({ data: "old-data", error: null, loading: true });

    await act(async () => {
      failure.reject(apiError);
      await refreshPromise;
    });
    expect(result.current).toMatchObject({ data: "old-data", error: apiError, loading: false });
  });

  it("切换项目时清除旧数据并忽略旧项目迟到结果", async () => {
    const a = deferred<string>();
    const b = deferred<string>();
    const loader = vi.fn((id: string) => (id === "project-a" ? a.promise : b.promise));
    const { result, rerender } = renderHook(
      ({ projectId }: { projectId: string }) => useProjectData(projectId, loader),
      { initialProps: { projectId: "project-a" } },
    );
    await waitFor(() => expect(loader).toHaveBeenCalledWith("project-a"));

    rerender({ projectId: "project-b" });
    await waitFor(() => expect(loader).toHaveBeenCalledWith("project-b"));
    expect(result.current).toMatchObject({ data: null, error: null, loading: true });

    await act(async () => {
      a.resolve("late-a");
      await a.promise;
    });
    expect(result.current).toMatchObject({ data: null, error: null, loading: true });

    await act(async () => {
      b.resolve("data-b");
      await b.promise;
    });
    expect(result.current).toMatchObject({ data: "data-b", error: null, loading: false });
  });

  it("仅在显式选择时 toast，且未知 request id 不进入提示", async () => {
    const { result } = renderHook(() =>
      useProjectData("proj-toast", vi.fn().mockRejectedValue(new TypeError("raw")), { toastOnError: true }),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(mocks.toast.toastError).toHaveBeenCalledWith("请求失败 (UNKNOWN_ERROR)", undefined);
  });

  it("较旧请求的迟到失败不会覆盖较新成功结果", async () => {
    const first = deferred<string>();
    const second = deferred<string>();
    const loader = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { result } = renderHook(() => useProjectData("proj-race", loader));

    await waitFor(() => expect(loader).toHaveBeenCalledTimes(1));
    let latestRefresh!: Promise<void>;
    act(() => {
      latestRefresh = result.current.refresh();
    });
    await waitFor(() => expect(loader).toHaveBeenCalledTimes(2));

    await act(async () => {
      second.resolve("latest");
      await latestRefresh;
    });
    expect(result.current.data).toBe("latest");

    await act(async () => {
      first.reject(new ApiError({ code: "STALE", message: "stale", requestId: "rid-stale", status: 500 }));
      await first.promise.catch(() => undefined);
    });

    expect(result.current.data).toBe("latest");
    expect(result.current.error).toBeNull();
    expect(mocks.toast.toastError).not.toHaveBeenCalledWith("stale (STALE)", "rid-stale");
  });

  it("projectId 清空时重置 data/error/loading", async () => {
    const apiError = new ApiError({ code: "FAILED", message: "failed", requestId: "rid-failed", status: 500 });
    const loader = vi.fn().mockRejectedValue(apiError);
    const { result, rerender } = renderHook(
      ({ projectId }: { projectId: string | undefined }) => useProjectData(projectId, loader),
      { initialProps: { projectId: "proj-clear" as string | undefined } },
    );

    await waitFor(() => expect(result.current.error).toBe(apiError));
    act(() => result.current.setData("stale"));
    rerender({ projectId: undefined });

    await waitFor(() => {
      expect(result.current).toMatchObject({ data: null, error: null, loading: false });
    });
  });
});
