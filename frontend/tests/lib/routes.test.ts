import { describe, expect, it } from "vitest";

import { resolveRouteMeta } from "@/lib/routes";
import { UI_COPY } from "@/lib/uiCopy";

describe("resolveRouteMeta", () => {
  it("resolves entries/import/prompt-templates route titles", () => {
    expect(resolveRouteMeta("/projects/demo/entries")).toEqual({
      title: UI_COPY.nav.entries,
      layout: "paper",
    });
    expect(resolveRouteMeta("/projects/demo/import")).toEqual({
      title: UI_COPY.nav.dataImport,
      layout: "tool",
    });
    expect(resolveRouteMeta("/projects/demo/prompt-templates")).toEqual({
      title: UI_COPY.nav.promptTemplates,
      layout: "tool",
    });
  });

  it("falls back to app name for unknown paths", () => {
    expect(resolveRouteMeta("/projects/demo/unknown")).toEqual({
      title: UI_COPY.brand.appName,
      layout: "tool",
    });
  });
});
