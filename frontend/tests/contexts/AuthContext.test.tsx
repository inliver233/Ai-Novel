// @vitest-environment jsdom
/**
 * AuthContext D 类 happy-path 交互测试（诚实镜像：断言【当前正确行为】，应绿）。
 *
 * 覆盖核心交互链路：
 *  1. login 成功 → user 状态置位、user 信息可访问
 *  2. logout → user 清空、回到未认证
 *  3. 认证态收到 401 未授权（ainovel:unauthorized 事件总线）→ 强制回到未认证
 *  4. 会话刷新定时器：递归 setTimeout 推进时间后触发 refreshSession
 *
 * 与 tests/contexts/authRefreshSchedule.test.ts 不重叠：后者仅测纯函数
 * computeNextAuthRefreshDelayMs；本测试覆盖 Provider 交互层（state/事件/定时器）。
 *
 * mock 方式：vi.mock("@/services/apiClient") —— 重新导出真实 ApiError 类，apiJson 为
 * vi.fn 按 path 路由（参考 ImportPage.stall.known_issue.test.tsx 的同款模式）。
 * 因此未使用 setup.ts 的 makeJsonResponse/makeApiErrorResponse（那是 fetch 传输层桩，
 * 与模块级 mock 互斥；模块 mock 下 401 直接抛真实 ApiError(status:401)）。
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// vi.mock 工厂会被提升到文件顶部执行，故用 vi.hoisted 声明 apiJson 桩，避免 TDZ
// （参考 ThemeToggle.test.tsx / ImportPage.stall.known_issue.test.tsx）。
const mocks = vi.hoisted(() => ({
  apiJson: vi.fn(),
}));

vi.mock("@/services/apiClient", () => ({
  // 重新导出真实形态的 ApiError：AuthContext 用 `e instanceof ApiError` 判错，
  // 此处与测试内 new ApiError(...) 为同一引用，instanceof 成立。
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
  apiJson: mocks.apiJson,
}));

import { ApiError } from "@/services/apiClient";
import { AuthProvider } from "@/contexts/AuthContext";
import { useAuth } from "@/contexts/auth";

// 认证成功响应信封（ApiOkPayload 形态）。expire_at=null → 刷新延迟取默认 5min。
function authUserOk(overrides: { id?: string; display_name?: string; is_admin?: boolean } = {}): {
  ok: true;
  data: { user: { id: string; display_name: string; is_admin: boolean }; session: { expire_at: number | null } };
  request_id: string;
} {
  return {
    ok: true,
    data: {
      user: { id: "u-1", display_name: "测试用户", is_admin: true, ...overrides },
      session: { expire_at: null },
    },
    request_id: "rid-user",
  };
}

function unauthorized401(): ApiError {
  return new ApiError({ code: "UNAUTHORIZED", message: "未登录", requestId: "rid-401", status: 401 });
}

// 最小消费者：渲染 auth 状态 + 暴露 login/logout 动作按钮。
// 通过 useAuth() 读取上下文，断言结构/状态而非中文 UI 文案快照。
function AuthProbe() {
  const auth = useAuth();
  return (
    <div>
      <span data-testid="status">{auth.status}</span>
      <span data-testid="user-id">{auth.user?.id ?? ""}</span>
      <span data-testid="user-name">{auth.user?.displayName ?? ""}</span>
      <span data-testid="is-admin">{String(auth.user?.isAdmin ?? false)}</span>
      <button onClick={() => void auth.login({ userId: "u1", password: "pw" })}>login</button>
      <button onClick={() => void auth.logout()}>logout</button>
    </div>
  );
}

describe("AuthContext 交互（D 类 happy-path）", () => {
  beforeEach(() => {
    mocks.apiJson.mockReset();
    localStorage.clear();
  });

  afterEach(() => {
    // 显式卸载 React 树：AuthProvider 挂载了刷新定时器与 unauthorized 事件监听，
    // 必须卸载以触发 effect cleanup，避免跨用例污染（setup.ts 仅清空 body 不卸载 React）。
    cleanup();
    vi.useRealTimers();
  });

  it("login 成功后置位 user 状态并可访问 user 信息", async () => {
    // 初始 mount refresh → 401（未认证），以便观察 login 的状态翻转。
    mocks.apiJson.mockImplementation((path: string) => {
      if (path === "/api/auth/user") return Promise.reject(unauthorized401());
      if (path === "/api/auth/local/login") return Promise.resolve(authUserOk());
      return Promise.resolve({ ok: true, data: {}, request_id: "rid" });
    });

    render(
      <AuthProvider>
        <AuthProbe />
      </AuthProvider>,
    );

    // 前置：初始 refresh 落定 unauthenticated（即 refresh 的 401 路径）。
    expect(await screen.findByTestId("status")).toHaveTextContent("unauthenticated");

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "login" }));

    // login → setUser(authenticated) → user 信息可访问。
    expect(await screen.findByTestId("status")).toHaveTextContent("authenticated");
    expect(screen.getByTestId("user-id")).toHaveTextContent("u-1");
    expect(screen.getByTestId("is-admin")).toHaveTextContent("true");
  });

  it("logout 后 user 清空、回到未认证", async () => {
    // 首次 /api/auth/user 成功（authenticated），logout 后再次 /api/auth/user → 401。
    let sessionAlive = true;
    mocks.apiJson.mockImplementation((path: string) => {
      if (path === "/api/auth/user") {
        if (sessionAlive) {
          sessionAlive = false;
          return Promise.resolve(authUserOk());
        }
        return Promise.reject(unauthorized401());
      }
      if (path === "/api/auth/logout") {
        sessionAlive = false;
        return Promise.resolve({ ok: true, data: {}, request_id: "rid-logout" });
      }
      return Promise.resolve({ ok: true, data: {}, request_id: "rid" });
    });

    render(
      <AuthProvider>
        <AuthProbe />
      </AuthProvider>,
    );

    // 前置：已认证、user 已置位。
    expect(await screen.findByTestId("user-id")).toHaveTextContent("u-1");

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "logout" }));

    // logout → POST /logout → refresh(silent) → /api/auth/user 401 → unauthenticated。
    expect(await screen.findByTestId("status")).toHaveTextContent("unauthenticated");
    expect(screen.getByTestId("user-id")).toHaveTextContent("");
  });

  it("认证态收到 401 未授权事件后清空 user 回到未认证", async () => {
    mocks.apiJson.mockImplementation((path: string) => {
      if (path === "/api/auth/user") return Promise.resolve(authUserOk());
      return Promise.resolve({ ok: true, data: {}, request_id: "rid" });
    });

    render(
      <AuthProvider>
        <AuthProbe />
      </AuthProvider>,
    );

    // 前置：已认证。findBy 命中 DOM 后再显式 flush 一个宏任务，确保 statusRef 的
    // passive useEffect 已提交（findBy 可能先于 deferred useEffect 命中 DOM）。
    expect(await screen.findByTestId("status")).toHaveTextContent("authenticated");
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    // 模拟 apiClient 在任意请求收到 401 时派发的 ainovel:unauthorized 事件
    // （真实 apiClient.apiJson 命中 401 会 window.dispatchEvent 此事件）。
    act(() => {
      window.dispatchEvent(new Event("ainovel:unauthorized"));
    });

    expect(screen.getByTestId("status")).toHaveTextContent("unauthenticated");
    expect(screen.getByTestId("user-id")).toHaveTextContent("");
  });

  it("会话刷新定时器递归触发 refreshSession", async () => {
    // expire_at=null → computeNextAuthRefreshDelayMs 返回默认 5*60_000ms。
    vi.useFakeTimers();

    const refreshCalls = vi.fn();
    mocks.apiJson.mockImplementation((path: string) => {
      if (path === "/api/auth/user") return Promise.resolve(authUserOk());
      if (path === "/api/auth/refresh") {
        refreshCalls();
        return Promise.resolve({
          ok: true,
          data: { refreshed: true, session: { expire_at: null } },
          request_id: "rid-refresh",
        });
      }
      return Promise.resolve({ ok: true, data: {}, request_id: "rid" });
    });

    render(
      <AuthProvider>
        <AuthProbe />
      </AuthProvider>,
    );

    // 刷新初始 mount 的 refresh 微任务，落定 authenticated；定时器已注册但未到点。
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(screen.getByTestId("status")).toHaveTextContent("authenticated");
    expect(refreshCalls).not.toHaveBeenCalled();

    // 推进 5 分钟 → 定时器触发首次 refreshSession。
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000);
    });
    expect(refreshCalls).toHaveBeenCalledTimes(1);

    // 再推进 5 分钟 → scheduleNext 递归再次触发。
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000);
    });
    expect(refreshCalls).toHaveBeenCalledTimes(2);
  });
});
