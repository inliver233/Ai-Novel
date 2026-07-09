import { describe, expect, it } from "vitest";

import { formatWritingDisabledReason, WRITING_RUNTIME_COPY } from "@/components/writing/writingRuntimeCopy";

describe("writingRuntimeCopy", () => {
  it("keeps shared prompt override and bundle safety copy stable", () => {
    expect(WRITING_RUNTIME_COPY.promptOverrideWarning).toBe(
      "已启用 Prompt 覆盖：生成将使用覆盖文本（可随时回退默认）。",
    );
    expect(WRITING_RUNTIME_COPY.promptOverridePersistenceHint).toContain("生成/追加生成");
    expect(WRITING_RUNTIME_COPY.bundleSafetyHint).toContain("API Key");
    expect(WRITING_RUNTIME_COPY.previewBundleSafetyHint).toContain("下载预览 bundle");
  });

  it("formats disabled reasons consistently across writing runtime panels", () => {
    expect(formatWritingDisabledReason("embedding_missing")).toBe("disabled: embedding_missing");
    expect(formatWritingDisabledReason(null)).toBe("disabled: unknown");
  });
});
