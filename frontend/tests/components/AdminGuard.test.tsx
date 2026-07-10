// @vitest-environment jsdom
import { render, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  auth: {
    status: "loading" as "loading" | "authenticated" | "dev_fallback" | "unauthenticated",
    user: null as { id: string; displayName: string; isAdmin: boolean } | null,
    session: null,
    refresh: vi.fn(),
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
  },
  guardedPageRender: vi.fn(),
}));

vi.mock("@/contexts/auth", () => ({ useAuth: () => mocks.auth }));

import { AdminGuard } from "@/components/layout/AdminGuard";
import { AuthGuard } from "@/components/layout/AuthGuard";
import { UI_COPY } from "@/lib/uiCopy";

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
}

function GuardedAdminPage() {
  mocks.guardedPageRender();
  return <div data-testid="guarded-admin-page" />;
}

function renderGuardStack() {
  return render(
    <MemoryRouter initialEntries={["/admin/users"]}>
      <LocationProbe />
      <Routes>
        <Route element={<AuthGuard />}>
          <Route element={<AdminGuard />}>
            <Route path="admin/users" element={<GuardedAdminPage />} />
          </Route>
        </Route>
        <Route path="login" element={<div data-testid="login-page" />} />
        <Route path="/" element={<div data-testid="home-page" />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mocks.auth.status = "loading";
  mocks.auth.user = null;
  mocks.guardedPageRender.mockClear();
});

describe("AuthGuard + AdminGuard", () => {
  it("loading 时保持目标地址且不挂载管理页", () => {
    const view = renderGuardStack();

    expect(view.getByTestId("location")).toHaveTextContent("/admin/users");
    expect(view.getByText(UI_COPY.common.loading)).toBeInTheDocument();
    expect(mocks.guardedPageRender).not.toHaveBeenCalled();
  });

  it("未认证时保留 next 参数并跳转登录", async () => {
    mocks.auth.status = "unauthenticated";
    const view = renderGuardStack();

    await waitFor(() => {
      expect(view.getByTestId("location")).toHaveTextContent("/login?next=%2Fadmin%2Fusers");
    });
    expect(view.getByTestId("login-page")).toBeInTheDocument();
    expect(mocks.guardedPageRender).not.toHaveBeenCalled();
  });

  it("已认证非管理员跳转首页且不挂载管理页", async () => {
    mocks.auth.status = "authenticated";
    mocks.auth.user = { id: "u-user", displayName: "User", isAdmin: false };
    const view = renderGuardStack();

    await waitFor(() => expect(view.getByTestId("location")).toHaveTextContent("/"));
    expect(view.getByTestId("home-page")).toBeInTheDocument();
    expect(mocks.guardedPageRender).not.toHaveBeenCalled();
  });

  it("dev fallback 用户无法进入管理员页面", async () => {
    mocks.auth.status = "dev_fallback";
    mocks.auth.user = { id: "local-user", displayName: "Local", isAdmin: false };
    const view = renderGuardStack();

    await waitFor(() => expect(view.getByTestId("location")).toHaveTextContent("/"));
    expect(mocks.guardedPageRender).not.toHaveBeenCalled();
  });

  it("已认证管理员挂载管理页并保持地址", () => {
    mocks.auth.status = "authenticated";
    mocks.auth.user = { id: "u-admin", displayName: "Admin", isAdmin: true };
    const view = renderGuardStack();

    expect(view.getByTestId("location")).toHaveTextContent("/admin/users");
    expect(view.getByTestId("guarded-admin-page")).toBeInTheDocument();
    expect(mocks.guardedPageRender).toHaveBeenCalledTimes(1);
  });
});
