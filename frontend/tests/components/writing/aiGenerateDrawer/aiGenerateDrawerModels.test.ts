import { describe, expect, it } from "vitest";

import type { GenerateForm } from "@/components/writing/types";
import {
  AI_GENERATE_CONTEXT_TOGGLES,
  getAiGenerateDrawerState,
  getStyleHelperText,
} from "@/components/writing/aiGenerateDrawer/aiGenerateDrawerModels";

function makeForm(overrides: Partial<GenerateForm> = {}): GenerateForm {
  return {
    instruction: "demo",
    target_word_count: 2000,
    macro_seed: "",
    prompt_override: null,
    stream: false,
    style_id: null,
    memory_injection_enabled: true,
    previous_mode: "full",
    rag_enabled: true,
    context: {
      include_world_setting: true,
      include_style_guide: true,
      include_constraints: true,
      include_outline: true,
      include_smart_context: true,
      require_sequential: false,
      character_ids: [],
      entry_ids: [],
    },
    ...overrides,
  };
}

describe("aiGenerateDrawerModels", () => {
  it("derives preset summary and prompt override flags", () => {
    const hasPreset = getAiGenerateDrawerState({
      genForm: makeForm(),
      preset: { project_id: "p1", provider: "openai", model: "gpt-test", stop: [], extra: {} },
    });

    expect(hasPreset.hasPromptOverride).toBe(false);
    expect(hasPreset.presetSummary).toBe("openai / gpt-test");
  });

  it("derives prompt override state and preserves dynamic style context", () => {
    const state = getAiGenerateDrawerState({
      genForm: makeForm({ prompt_override: { user: "override" } }),
      preset: null,
    });

    expect(state.hasPromptOverride).toBe(true);
    expect(state.presetSummary).toEqual(expect.any(String));
    expect(state.presetSummary).not.toHaveLength(0);

    const namedStyleText = getStyleHelperText("noir-sentinel", null);
    expect(namedStyleText).toContain("noir-sentinel");
    expect(namedStyleText).not.toContain("null");

    const failedStyleText = getStyleHelperText(null, "E_STYLE_SENTINEL");
    expect(failedStyleText).toContain("E_STYLE_SENTINEL");
    expect(failedStyleText).not.toContain("null");
  });

  it("keeps mapped context toggles deterministic", () => {
    expect(AI_GENERATE_CONTEXT_TOGGLES.map((item) => item.key)).toEqual([
      "include_world_setting",
      "include_style_guide",
      "include_constraints",
      "include_outline",
      "include_smart_context",
      "require_sequential",
    ]);
  });
});
