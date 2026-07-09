import { describe, expect, it } from "vitest";

import {
  APP_SHELL_PRIMARY_PROJECT_NAV_GROUPS,
  APP_SHELL_PROJECT_NAV_ITEMS,
  getAppShellProjectNavItems,
} from "@/components/layout/appShellNavConfig";

describe("appShellNavConfig", () => {
  it("keeps deterministic group order for primary navigation", () => {
    expect(APP_SHELL_PRIMARY_PROJECT_NAV_GROUPS).toEqual(["workbench", "view", "aiConfig"]);
  });

  it("ensures each nav item id and route are unique", () => {
    const projectId = "demo-project";
    const ids = APP_SHELL_PROJECT_NAV_ITEMS.map((item) => item.id);
    const routes = APP_SHELL_PROJECT_NAV_ITEMS.map((item) => item.to(projectId));

    expect(new Set(ids).size).toBe(ids.length);
    expect(new Set(routes).size).toBe(routes.length);
  });

  it("keeps every group non-empty and includes critical entries", () => {
    const workbench = getAppShellProjectNavItems("workbench").map((item) => item.id);
    const view = getAppShellProjectNavItems("view").map((item) => item.id);
    const aiConfig = getAppShellProjectNavItems("aiConfig").map((item) => item.id);
    expect(workbench.length).toBeGreaterThan(0);
    expect(view.length).toBeGreaterThan(0);
    expect(aiConfig.length).toBeGreaterThan(0);

    expect(workbench).toContain("writing");
    expect(view).toContain("preview");
    expect(aiConfig).toContain("prompts");
  });
});
