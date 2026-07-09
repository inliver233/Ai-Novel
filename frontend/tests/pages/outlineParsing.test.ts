import { describe, expect, it } from "vitest";

import { deriveOutlineFromStoredContent, parseOutlineGenResultFromText } from "@/pages/outlineParsing";

const RAW_PAYLOAD = JSON.stringify(
  {
    outline_md: "# 整体梗概\n- 手工粘贴的大纲应该被识别",
    chapters: [
      { number: 1, title: "开篇", beats: ["主角醒来", "系统出现"] },
      { number: 2, title: "立足", beats: ["开始营业"] },
    ],
  },
  null,
  2,
);

const RAW_VOLUME_PAYLOAD = JSON.stringify(
  {
    outline_md: "# 分卷梗概\n- 新格式也应该被识别",
    volumes: [{ number: 1, title: "第一卷", summary: "卷摘要" }],
  },
  null,
  2,
);

describe("outlineParsing", () => {
  it("parses outline payload pasted as raw text", () => {
    const parsed = parseOutlineGenResultFromText(RAW_PAYLOAD);

    expect(parsed?.outline_md).toContain("整体梗概");
    expect(parsed?.chapters).toHaveLength(2);
    expect(parsed?.chapters[1]?.number).toBe(2);
  });

  it("derives normalized outline content and chapters from legacy stored json text", () => {
    const derived = deriveOutlineFromStoredContent(RAW_PAYLOAD, null);

    expect(derived.normalizedContentMd).toContain("整体梗概");
    expect(derived.normalizedContentMd.trim().startsWith("{")).toBe(false);
    expect(derived.chapters).toHaveLength(2);
  });

  it("prefers stored structure when it already exists", () => {
    const derived = deriveOutlineFromStoredContent("plain markdown", {
      chapters: [{ number: 9, title: "已存结构", beats: ["x"] }],
    });

    expect(derived.normalizedContentMd).toBe("plain markdown");
    expect(derived.chapters).toHaveLength(1);
    expect(derived.chapters[0]?.number).toBe(9);
  });

  it("parses volume-based outline payload pasted as raw text", () => {
    const parsed = parseOutlineGenResultFromText(RAW_VOLUME_PAYLOAD);

    expect(parsed?.outline_md).toContain("分卷梗概");
    expect(parsed?.volumes).toHaveLength(1);
    expect(parsed?.volumes[0]?.title).toBe("第一卷");
    expect(parsed?.chapters).toHaveLength(1);
  });

  it("prefers stored volumes when structure already uses the new format", () => {
    const derived = deriveOutlineFromStoredContent("plain markdown", {
      volumes: [{ number: 1, title: "第一卷", summary: "卷摘要" }],
      chapters: [],
    });

    expect(derived.normalizedContentMd).toBe("plain markdown");
    expect(derived.volumes).toHaveLength(1);
    expect(derived.volumes[0]?.summary).toBe("卷摘要");
    expect(derived.chapters).toHaveLength(1);
  });
});
