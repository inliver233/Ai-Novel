import { describe, expect, it } from "vitest";

import type { LLMProfile } from "@/types";

import { describeModelListState, deriveLlmModuleAccessState } from "@/components/prompts/llmConnectionState";

const baseProfile: LLMProfile = {
  id: "prof-1",
  owner_user_id: "u1",
  name: "默认配置",
  provider: "openai_compatible",
  base_url: "https://mock.example/v1",
  model: "gpt-4o-mini",
  has_api_key: true,
  masked_api_key: "sk-****1234",
  created_at: "2026-03-13T00:00:00Z",
  updated_at: "2026-03-13T00:00:00Z",
};

describe("llmConnectionState", () => {
  it("blocks the main module when no profile is bound", () => {
    const state = deriveLlmModuleAccessState({
      scope: "main",
      moduleProvider: "openai_compatible",
      selectedProfile: null,
    });
    expect(state.stage).toBe("missing_profile");
    expect(state.tone).toBe("warning");
    expect(state.actionReason).toEqual(expect.any(String));
    expect(state.effectiveProfile).toBeNull();
  });

  it("blocks when the effective profile has no key", () => {
    const state = deriveLlmModuleAccessState({
      scope: "main",
      moduleProvider: "openai_compatible",
      selectedProfile: { ...baseProfile, has_api_key: false, masked_api_key: null },
    });
    expect(state.stage).toBe("missing_key");
    expect(state.tone).toBe("warning");
    expect(state.actionReason).toEqual(expect.any(String));
    expect(state.effectiveProfile).toEqual(expect.objectContaining({ id: baseProfile.id }));
  });

  it("explains fallback provider mismatch for task modules", () => {
    const state = deriveLlmModuleAccessState({
      scope: "task",
      moduleProvider: "anthropic",
      selectedProfile: baseProfile,
      boundProfile: null,
    });
    expect(state.stage).toBe("provider_mismatch");
    expect(state.tone).toBe("warning");
    expect(state.actionReason).toEqual(expect.any(String));
    expect(state.effectiveProfile).toEqual(expect.objectContaining({ id: baseProfile.id }));
  });

  it("describes empty remote model lists after a successful request", () => {
    const access = deriveLlmModuleAccessState({
      scope: "main",
      moduleProvider: "openai_compatible",
      selectedProfile: baseProfile,
    });
    const requestedEmptyState = {
      loading: false,
      options: [],
      warning: null,
      error: null,
      requestId: "rid-1",
    };
    const text = describeModelListState(requestedEmptyState, access);
    const untouchedText = describeModelListState({ ...requestedEmptyState, requestId: null }, access);

    expect(text).toEqual(expect.any(String));
    expect(text).not.toHaveLength(0);
    expect(text).not.toBe(untouchedText);
  });
});
