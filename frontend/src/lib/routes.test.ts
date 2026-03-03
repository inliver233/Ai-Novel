import { describe, expect, it } from "vitest";

import { resolveRouteMeta } from "./routes";
import { UI_COPY } from "./uiCopy";

describe("resolveRouteMeta", () => {
  it("resolves foreshadows/import/prompt-templates route titles", () => {
    expect(resolveRouteMeta("/projects/demo/foreshadows")).toEqual({
      title: UI_COPY.nav.foreshadows,
      layout: "tool",
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
