import { describe, expect, it } from "vitest";

import {
  appendCappedRawText,
  buildGeneratedOutlineTitle,
  buildNextOutlineTitle,
  toFinalPreviewJson,
} from "@/pages/outline/outlineModels";

describe("outlineModels", () => {
  it("caps streamed raw text and preserves a truncation prefix", () => {
    const first = appendCappedRawText("", "abcdef", 4);
    const second = appendCappedRawText(first, "gh", 4);

    expect(first).toBe("[raw 已截断前 2 字符，仅保留最近 4 字符]\ncdef");
    expect(second).toBe("[raw 已截断前 4 字符，仅保留最近 4 字符]\nefgh");
  });

  it("builds stable outline titles for local create flows", () => {
    expect(buildNextOutlineTitle(0)).toBe("大纲 v1");
    expect(buildNextOutlineTitle(3)).toBe("大纲 v4");
    expect(buildGeneratedOutlineTitle(new Date("2026-03-14T01:35:00Z"))).toBe("AI 大纲 2026-03-14 01:35");
  });

  it("includes volumes in the final preview json", () => {
    const preview = toFinalPreviewJson({
      outline_md: "# 大纲",
      volumes: [{ number: 1, title: "第一卷", summary: "卷摘要" }],
      chapters: [{ number: 1, title: "第一章", beats: ["推进"] }],
      raw_output: "{}",
    });

    expect(preview).toContain('"volumes"');
    expect(preview).toContain('"第一卷"');
  });
});
