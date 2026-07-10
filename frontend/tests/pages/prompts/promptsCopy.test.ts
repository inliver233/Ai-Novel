import { describe, expect, it } from "vitest";

import { buildClearTaskApiKeyConfirm, buildDeleteTaskModuleConfirm } from "@/pages/prompts/promptsCopy";

describe("promptsCopy", () => {
  it("builds the delete-task confirmation with the task label", () => {
    const taskLabel = "task-label-sentinel";
    const confirmation = buildDeleteTaskModuleConfirm(taskLabel);

    expect(confirmation.description).toContain(taskLabel);
    expect(confirmation.title).toEqual(expect.any(String));
    expect(confirmation.confirmText).toEqual(expect.any(String));
  });

  it("builds the shared-profile clear confirmation with the profile name", () => {
    const profileName = "profile-name-sentinel";
    const confirmation = buildClearTaskApiKeyConfirm(profileName);

    expect(confirmation.description).toContain(profileName);
    expect(confirmation.title).toEqual(expect.any(String));
    expect(confirmation.confirmText).toEqual(expect.any(String));
  });
});
