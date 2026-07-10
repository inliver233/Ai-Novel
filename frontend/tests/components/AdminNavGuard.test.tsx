// @vitest-environment jsdom
// M42/frontend-arch#11 回归：侧边栏管理入口必须与当前认证用户的 isAdmin 权限一致。
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// 非 admin 用户桩：is_admin=false。vi.mock 工厂提升执行，用 vi.hoisted 声明避免 TDZ（参考 ThemeToggle.test.tsx）。
const nonAdminAuth = vi.hoisted(() => ({
  status: "authenticated" as const,
  user: { id: "u-non-admin", displayName: "普通用户", isAdmin: false },
  session: { expireAt: null as number | null },
  refresh: vi.fn(),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
}));

vi.mock("@/contexts/auth", () => ({
  // AppShell 仅消费 useAuth；返回非 admin 已认证用户以触发导航渲染分支。
  useAuth: () => nonAdminAuth,
}));

// ProjectSwitcher 依赖 ProjectsContext（会触发项目列表拉取），桩掉以隔离导航渲染逻辑。
vi.mock("@/components/atelier/ProjectSwitcher", () => ({
  ProjectSwitcher: () => null,
}));

// ThemeToggle 依赖 @/services/theme，桩掉避免主题服务副作用。
vi.mock("@/components/atelier/ThemeToggle", () => ({
  ThemeToggle: () => null,
}));

import { AppShell } from "@/components/layout/AppShell";
import { UI_COPY } from "@/lib/uiCopy";

beforeEach(() => {
  nonAdminAuth.user.isAdmin = false;
});

describe("admin navigation visibility", () => {
  it("非 admin 用户不渲染用户管理入口", () => {
    const view = render(
      <MemoryRouter initialEntries={["/"]}>
        <AppShell />
      </MemoryRouter>,
    );

    // 正确行为：非 admin 用户根本不应渲染 /admin/users 导航入口（defense-in-depth，
    // 即便页面层/后端有校验，导航层也应在非 admin 用户下屏蔽该入口）。
    // 按 href 断言（不锁中文文案，§6.6 规则 2）：该入口不应进入 DOM。
    const adminUsersLinks = view.container.querySelectorAll('a[href="/admin/users"]');

    expect(adminUsersLinks).toHaveLength(0);
    fireEvent.click(view.getByRole("button", { name: UI_COPY.nav.openNav }));
    expect(view.container.querySelectorAll('a[href="/admin/users"]')).toHaveLength(0);
  });

  it("admin 用户渲染用户管理入口", () => {
    nonAdminAuth.user.isAdmin = true;
    const view = render(
      <MemoryRouter initialEntries={["/"]}>
        <AppShell />
      </MemoryRouter>,
    );

    expect(view.container.querySelectorAll('a[href="/admin/users"]')).toHaveLength(1);
    fireEvent.click(view.getByRole("button", { name: UI_COPY.nav.openNav }));
    expect(view.container.querySelectorAll('a[href="/admin/users"]')).toHaveLength(2);
  });
});
