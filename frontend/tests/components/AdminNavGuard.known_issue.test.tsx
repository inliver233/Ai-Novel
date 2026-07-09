// @vitest-environment jsdom
// M42 known_issue：/admin/users 路由无路由级权限守卫，侧边栏"用户管理"导航入口对所有登录用户无条件渲染。
// AppShell.tsx 桌面侧边栏（:401-407）与移动端导航（:318-325）均无条件渲染 to="/admin/users" 的
// SidebarLink，仅靠 AdminUsersPage 页面层 + 后端 server-side 检查拦截，缺 defense-in-depth。
// 正确行为：非 admin 用户不应看到"用户管理"导航入口。当前实现有 bug → 本测试必须 FAILED(红)。
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
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

describe("AdminNavGuard (M42 known_issue)", () => {
  it(
    "非 admin 用户不应看到'用户管理'导航入口",
    { tags: ["@known_issue"] },
    () => {
      render(
        <MemoryRouter initialEntries={["/"]}>
          <AppShell />
        </MemoryRouter>,
      );

      // 正确行为：非 admin 用户根本不应渲染"用户管理"导航入口（defense-in-depth，
      // 即便页面层/后端有校验，导航层也应在非 admin 用户下屏蔽该入口）。hidden:true
      // 覆盖桌面侧边栏 aside 在 CSS 加载场景下 display:none 的情况，断言该入口不应进入 DOM。
      const adminUsersLinks = screen.queryAllByRole("link", {
        name: /用户管理/,
        hidden: true,
      });

      expect(adminUsersLinks).toHaveLength(0);
    },
  );
});
