import { describe, expect, it } from "vitest";

import type { GenerateForm } from "@/components/writing/types";
import { AI_GENERATE_CONTEXT_TOGGLES, getAiGenerateDrawerState, getStyleHelperText } from "@/components/writing/aiGenerateDrawer/aiGenerateDrawerModels";

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

  it("keeps prompt override and style helper text copy stable", () => {
    const state = getAiGenerateDrawerState({
      genForm: makeForm({ prompt_override: { user: "override" } }),
      preset: null,
    });

    expect(state.hasPromptOverride).toBe(true);
    expect(state.presetSummary).toBe("未加载 LLM 配置");
    expect(getStyleHelperText("项目默认风格", null)).toBe("项目默认：项目默认风格");
    expect(getStyleHelperText(null, "E_STYLE")).toBe("项目默认：（未设置） | 加载失败：E_STYLE");
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
