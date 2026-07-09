import { describe, expect, it } from "vitest";

import { buildClearTaskApiKeyConfirm, buildDeleteTaskModuleConfirm, PROMPTS_COPY } from "@/pages/prompts/promptsCopy";

describe("promptsCopy", () => {
  it("keeps the prompts vector test-save hint stable", () => {
    expect(PROMPTS_COPY.vectorRag.saveBeforeTestHint).toContain("保存 RAG 配置");
  });

  it("builds the delete-task confirmation with the task label", () => {
    expect(buildDeleteTaskModuleConfirm("章节生成").description).toContain("章节生成");
  });

  it("builds the shared-profile clear confirmation with the profile name", () => {
    expect(buildClearTaskApiKeyConfirm("shared-profile").description).toContain("shared-profile");
  });
});
