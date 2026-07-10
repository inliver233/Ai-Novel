// @vitest-environment jsdom
// M42/frontend-arch#11：非管理员直达 /admin/users 时应在路由层被重定向，
// 不能先挂载 AdminUsersPage 再依赖页面内部自查。
import type { ReactNode } from "react";
import { render, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

const adminUsersPageRender = vi.hoisted(() => vi.fn());

const nonAdminAuth = vi.hoisted(() => ({
  status: "authenticated" as const,
  user: { id: "u-non-admin", displayName: "non-admin", isAdmin: false },
  session: { expireAt: null as number | null },
  refresh: vi.fn(),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
}));

vi.mock("@/contexts/auth", () => ({ useAuth: () => nonAdminAuth }));
vi.mock("@/contexts/AuthContext", () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("@/contexts/ProjectsContext", () => ({
  ProjectsProvider: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("@/components/ui/ToastProvider", () => ({
  ToastProvider: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("@/components/ui/ConfirmProvider", () => ({
  ConfirmProvider: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("@/components/layout/AuthGuard", async () => {
  const { Outlet } = await import("react-router-dom");
  return { AuthGuard: () => <Outlet /> };
});
vi.mock("@/components/layout/AppShell", async () => {
  const { Outlet } = await import("react-router-dom");
  return { AppShell: () => <Outlet /> };
});
vi.mock("@/pages/AdminUsersPage", () => ({
  AdminUsersPage: () => {
    adminUsersPageRender();
    return <div data-testid="admin-users-page" />;
  },
}));
vi.mock("@/pages/DashboardPage", () => ({ DashboardPage: () => <div data-testid="dashboard-page" /> }));

import { AdminGuard } from "@/components/layout/AdminGuard";

beforeEach(() => {
  nonAdminAuth.user.isAdmin = false;
  adminUsersPageRender.mockClear();
});

it("redirects a non-admin direct visit before mounting the admin page", async () => {
  window.history.replaceState({}, "", "/admin/users");
  const { default: App } = await import("@/App");

  const view = render(<App />);

  await waitFor(() => expect(window.location.pathname).toBe("/"));
  expect(view.queryByTestId("admin-users-page")).not.toBeInTheDocument();
  expect(adminUsersPageRender).not.toHaveBeenCalled();
});

it("renders the guarded child for an authenticated admin", () => {
  nonAdminAuth.user.isAdmin = true;
  const view = render(
    <MemoryRouter initialEntries={["/admin/users"]}>
      <Routes>
        <Route element={<AdminGuard />}>
          <Route path="admin/users" element={<div data-testid="guarded-admin-page" />} />
        </Route>
        <Route path="/" element={<div data-testid="home-page" />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(view.getByTestId("guarded-admin-page")).toBeInTheDocument();
  expect(view.queryByTestId("home-page")).not.toBeInTheDocument();
});
