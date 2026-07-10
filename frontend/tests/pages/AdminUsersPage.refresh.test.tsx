// @vitest-environment jsdom
// fe8-P8 回归：AdminUsersPage 创建用户后必须显式刷新列表。
// AdminUsersPage.tsx:225-229，createUser 成功后用 setSearchInput(userId); setSearchQuery(userId);
// setOnlineOnly(false); setCursor(null); setCursorHistory([]) 期望触发列表刷新。但若当前
// searchQuery 已等于将创建的 userId 且 cursor 已为 null、onlineOnly 已为 false（例如先搜过该
// userId），则 setSearchQuery / setCursor / setOnlineOnly 均为 React bail-out no-op（新值与旧值
// Object.is 相等）→ load 的 useCallback deps [cursor, onlineOnly, searchQuery, toast] 未变
// → load 回调身份不变 → useEffect([canManage, load]) 不重跑 → 列表不刷新，新用户不出现。
// （setCursorHistory([]) 虽产生新数组引用触发 re-render，但 cursorHistory 不在 load deps 内，
// 亦不影响该 effect。）
// 覆盖筛选状态未变化以及筛选状态变化两条路径。
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// fe8-P8：vi.mock 工厂会被提升到文件顶部执行，故用 vi.hoisted 声明桩，避免 TDZ（参考
// ThemeToggle.test.tsx / ImportPage.stall 测试）。toast / confirm / auth 必须返回稳定引用：
// load 的 useCallback deps 含 toast，若每次 render 返回新对象 → 回调身份变化 → 挂载 effect
// 每 render 重跑 → 无限循环（见 ImportPage stall 测试教训）。
const mocks = vi.hoisted(() => ({
  apiJson: vi.fn(),
  toast: {
    toastSuccess: vi.fn(),
    toastWarning: vi.fn(),
    toastError: vi.fn(),
  } as const,
  confirm: {
    confirm: vi.fn(),
    choose: vi.fn(),
  },
  auth: {
    status: "authenticated" as const,
    user: { id: "admin1", displayName: "管理员", isAdmin: true },
    session: { expireAt: null },
    refresh: vi.fn(),
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
  },
  // fe8-P8：标记 POST 创建用户是否已发生。翻转后，后续 GET 列表返回含新用户的列表
  // （模拟"若刷新则新用户可见"）。bug 导致刷新不触发 → GET 不再被调用 → 列表仍空。
  created: false,
}));

vi.mock("@/services/apiClient", () => ({
  // AdminUsersPage 仅用到 ApiError / apiJson；提供两者即可。
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

// useToast 需 ToastProvider；用 no-op mock 隔离，避免拉入 portal/全局副作用。
vi.mock("@/components/ui/toast", () => ({
  useToast: () => mocks.toast,
}));

// useConfirm 需 ConfirmProvider；createUser 流程不触发 confirm，提供稳定桩即可。
vi.mock("@/components/ui/confirm", () => ({
  useConfirm: () => mocks.confirm,
}));

// useAuth 需 AuthProvider；直接提供已认证管理员态，使 canManage=true。
vi.mock("@/contexts/auth", () => ({
  useAuth: () => mocks.auth,
}));

// fe8-P8：copyText 导入 react-dom/client 与 CopyFallbackModal，本流程不触发，
// mock 掉避免拉入不必要的副作用链。
vi.mock("@/lib/copyText", () => ({
  copyText: vi.fn().mockResolvedValue(true),
}));

import { AdminUsersPage } from "@/pages/AdminUsersPage";

// fe8-P8：将创建的新用户。id 即用作搜索关键词与创建 user_id，使"先搜后建"复现 bug。
const NEW_USER_ID = "newuser";
const newUser = {
  id: NEW_USER_ID,
  email: null,
  display_name: null,
  is_admin: false,
  disabled: false,
};

function emptyListResponse() {
  return {
    ok: true,
    data: {
      users: [],
      summary: {
        generated_at: null,
        online_window_seconds: 300,
        total_users: 0,
        total_admin_users: 0,
        total_disabled_users: 0,
        total_online_users: 0,
        filtered_total_users: 0,
        total_generation_calls: 0,
        total_generation_error_calls: 0,
        total_generated_chars: 0,
      },
      pagination: { limit: 50, cursor: null, next_cursor: null, has_more: false },
    },
    request_id: "rid-list",
  };
}

function userListWithNewResponse() {
  return {
    ok: true,
    data: {
      users: [newUser],
      summary: {
        generated_at: null,
        online_window_seconds: 300,
        total_users: 1,
        total_admin_users: 0,
        total_disabled_users: 0,
        total_online_users: 0,
        filtered_total_users: 1,
        total_generation_calls: 0,
        total_generation_error_calls: 0,
        total_generated_chars: 0,
      },
      pagination: { limit: 50, cursor: null, next_cursor: null, has_more: false },
    },
    request_id: "rid-list",
  };
}

describe("AdminUsersPage 创建用户后刷新", () => {
  beforeEach(() => {
    // fe8-P8：每个用例重置 created 标志并重装 apiJson 路由。
    mocks.created = false;
    mocks.apiJson.mockReset();
    mocks.toast.toastSuccess.mockClear();
    mocks.toast.toastWarning.mockClear();
    mocks.toast.toastError.mockClear();
    mocks.apiJson.mockImplementation((path: string, init?: { method?: string }) => {
      const method = init?.method ?? "GET";
      // POST 创建用户：翻转 created 标志，返回成功。
      if (method === "POST" && path === "/api/auth/admin/users") {
        mocks.created = true;
        return Promise.resolve({
          ok: true,
          data: { user: newUser, temp_password: "tmp-pwd-xyz" },
          request_id: "rid-create",
        });
      }
      // GET 列表：created 前 → 空；created 后 → 含新用户（模拟"若刷新则可见"）。
      if (method === "GET" && path.startsWith("/api/auth/admin/users?")) {
        return Promise.resolve(mocks.created ? userListWithNewResponse() : emptyListResponse());
      }
      return Promise.reject(new Error(`unexpected apiJson: ${method} ${path}`));
    });
  });

  it("先搜该 userId 再创建同名用户后，新用户应出现在列表中", async () => {
    const user = userEvent.setup();

    render(<AdminUsersPage />);

    // 等待初始挂载 load 完成（GET 已调用）。
    await waitFor(() => expect(mocks.apiJson).toHaveBeenCalled());

    // fe8-P8 bug 复现第一步：先搜索 newuser，使 searchQuery="newuser"、cursor=null。
    const searchInput = document.querySelector<HTMLInputElement>("#admin_users_search");
    expect(searchInput).not.toBeNull();
    await user.type(searchInput!, NEW_USER_ID);
    const searchSubmit = searchInput!.closest("form")!.querySelector<HTMLButtonElement>('button[type="submit"]');
    expect(searchSubmit).not.toBeNull();
    await user.click(searchSubmit!);
    // 搜索触发 load（q=newuser）→ 此时 created=false → 返回空列表。
    await waitFor(() => expect(mocks.apiJson).toHaveBeenCalledWith(expect.stringContaining("q=newuser")));

    // fe8-P8 bug 复现第二步：在创建表单填入同名 user_id 并提交。
    const userIdInput = document.querySelector<HTMLInputElement>("#admin_users_user_id");
    expect(userIdInput).not.toBeNull();
    await user.type(userIdInput!, NEW_USER_ID);
    const createSubmit = userIdInput!.closest("form")!.querySelector<HTMLButtonElement>('button[type="submit"]');
    expect(createSubmit).not.toBeNull();
    await user.click(createSubmit!);

    // 前置确认：createUser 已执行（POST 成功 → toastSuccess 被调用）。
    // 若此处失败说明测试搭建错误而非 bug。
    await waitFor(() => expect(mocks.toast.toastSuccess).toHaveBeenCalledWith(expect.any(String), "rid-create"));

    // 正确行为断言：创建用户后列表应刷新，新用户 newuser 应出现在列表中。
    // waitFor 轮询以给可能的刷新 effect 留出时间（honest mirror：若修复则 load 重跑、
    // GET 返回 [newuser]、列表渲染 newuser → 断言通过）。
    await waitFor(() => {
      expect(screen.getAllByText(NEW_USER_ID).length).toBeGreaterThan(0);
    });
  });

  it("默认筛选下创建用户只触发一次刷新", async () => {
    const user = userEvent.setup();
    render(<AdminUsersPage />);
    await waitFor(() => expect(mocks.apiJson).toHaveBeenCalledTimes(1));

    const userIdInput = document.querySelector<HTMLInputElement>("#admin_users_user_id");
    expect(userIdInput).not.toBeNull();
    await user.type(userIdInput!, NEW_USER_ID);
    const createSubmit = userIdInput!.closest("form")!.querySelector<HTMLButtonElement>('button[type="submit"]');
    expect(createSubmit).not.toBeNull();
    await user.click(createSubmit!);

    await waitFor(() => expect(screen.getAllByText(NEW_USER_ID).length).toBeGreaterThan(0));
    const listCalls = mocks.apiJson.mock.calls.filter(
      ([path, init]) => (init?.method ?? "GET") === "GET" && String(path).startsWith("/api/auth/admin/users?"),
    );
    expect(listCalls).toHaveLength(2);
    expect(listCalls[1]?.[0]).toContain("q=newuser");
  });
});
