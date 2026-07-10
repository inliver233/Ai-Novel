// @vitest-environment jsdom
/**
 * jsdom + @testing-library/react 交互测试样例（H43：此前 69 个组件零 DOM 交互测试）。
 *
 * 证明 vitest 现已具备：jsdom 环境、RTL 渲染、jest-dom 匹配器（toBeInTheDocument /
 * toHaveAccessibleName）、userEvent 真实点击、vi.mock 服务桩。后续组件/页面交互测试
 * 可照此模板扩充。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// vi.mock 工厂会被提升到文件顶部执行，故用 vi.hoisted 声明被引用的桩，避免 TDZ。
const themeMocks = vi.hoisted(() => ({
  readThemeState: () => null,
  writeThemeState: vi.fn(),
}));
vi.mock("@/services/theme", () => themeMocks);

import { ThemeToggle } from "@/components/atelier/ThemeToggle";

describe("ThemeToggle", () => {
  beforeEach(() => {
    themeMocks.writeThemeState.mockClear();
    document.documentElement.classList.remove("dark");
  });

  it("渲染带可访问名称的主题切换按钮", () => {
    render(<ThemeToggle />);
    const btn = screen.getByRole("button");
    expect(btn).toBeInTheDocument();
    expect(btn).toHaveAccessibleName();
    expect(themeMocks.writeThemeState).not.toHaveBeenCalled();
  });

  it("点击切换到暗色并持久化（调用 writeThemeState）", async () => {
    const user = userEvent.setup();
    render(<ThemeToggle />);
    const btn = screen.getByRole("button");
    const initialAccessibleName = btn.getAttribute("aria-label");

    await user.click(btn);

    expect(themeMocks.writeThemeState).toHaveBeenCalledWith(expect.objectContaining({ mode: "dark" }));
    expect(btn).toHaveAccessibleName();
    expect(btn.getAttribute("aria-label")).not.toBe(initialAccessibleName);
  });
});
