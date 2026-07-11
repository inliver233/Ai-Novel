import { readdirSync, readFileSync } from "node:fs";
import { extname, join, relative } from "node:path";

import { describe, expect, it } from "vitest";

const SRC_ROOT = join(process.cwd(), "src");

function sourceFiles(directory = SRC_ROOT): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return [".ts", ".tsx"].includes(extname(entry.name)) ? [path] : [];
  });
}

function matchingSources(pattern: RegExp, excluded: ReadonlySet<string> = new Set()): string[] {
  return sourceFiles().flatMap((path) => {
    const name = relative(SRC_ROOT, path).replaceAll("\\", "/");
    if (excluded.has(name)) return [];
    return pattern.test(readFileSync(path, "utf8")) ? [name] : [];
  });
}

describe("API error source contract", () => {
  it("forbids unsafe ApiError assertions and local normalizers", () => {
    expect(matchingSources(/\bas ApiError\b/)).toEqual([]);
    expect(matchingSources(/function\s+normalize(?:Api|ProjectData)Error\b/)).toEqual([]);
    expect(matchingSources(/function\s+toPromptStudioError\b/)).toEqual([]);
  });

  it("keeps the message/code template inside the shared presentation owner", () => {
    expect(
      matchingSources(/\$\{[^}]+\.message\}\s*\(\$\{[^}]+\.code\}\)/, new Set(["lib/apiErrorPresentation.ts"])),
    ).toEqual([]);
  });
});
