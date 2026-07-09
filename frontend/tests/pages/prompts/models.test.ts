import { describe, expect, it } from "vitest";

import type { LLMProfile, LLMPreset } from "@/types";
import {
  buildPresetPayload,
  DEFAULT_LLM_FORM,
  formFromPreset,
  formFromProfile,
  payloadEquals,
  payloadFromPreset,
} from "@/pages/prompts/models";

describe("prompts/models", () => {
  it("parses responses reasoning/text verbosity from extra", () => {
    const preset: LLMPreset = {
      project_id: "p1",
      provider: "openai_responses",
      base_url: "https://api.openai.com/v1",
      model: "gpt-5-mini",
      temperature: 0.7,
      top_p: 1,
      max_tokens: 4096,
      stop: [],
      timeout_seconds: 180,
      extra: {
        reasoning: { effort: "low" },
        text: { verbosity: "high", format: { type: "json_object" } },
      },
    };
    const form = formFromPreset(preset);
    expect(form.reasoning_effort).toBe("low");
    expect(form.text_verbosity).toBe("high");
  });

  it("builds payload and preserves non-managed extra fields", () => {
    const form = {
      ...DEFAULT_LLM_FORM,
      provider: "openai_responses" as const,
      model: "gpt-5-mini",
      reasoning_effort: "medium",
      text_verbosity: "low",
      extra: JSON.stringify({
        text: { format: { type: "json_schema", name: "x", schema: { type: "object" } } },
      }),
    };
    const out = buildPresetPayload(form);
    expect(out.ok).toBe(true);
    if (!out.ok) return;
    expect(out.payload.extra.reasoning).toEqual({ effort: "medium" });
    expect(out.payload.extra.text).toEqual({
      format: { type: "json_schema", name: "x", schema: { type: "object" } },
      verbosity: "low",
    });
  });

  it("rejects invalid anthropic thinking budget", () => {
    const form = {
      ...DEFAULT_LLM_FORM,
      provider: "anthropic" as const,
      model: "claude-3-7-sonnet-20250219",
      anthropic_thinking_enabled: true,
      anthropic_thinking_budget_tokens: "abc",
    };
    const out = buildPresetPayload(form);
    expect(out.ok).toBe(false);
  });

  it("payload roundtrip remains equal", () => {
    const preset: LLMPreset = {
      project_id: "p1",
      provider: "gemini",
      base_url: "https://generativelanguage.googleapis.com",
      model: "gemini-2.5-pro",
      temperature: 0.2,
      top_p: 0.8,
      max_tokens: 2048,
      top_k: 40,
      stop: ["###"],
      timeout_seconds: 120,
      extra: {
        thinkingConfig: { thinkingBudget: 512, includeThoughts: true },
      },
    };
    const payloadA = payloadFromPreset(preset);
    const form = formFromPreset(preset);
    const payloadB = buildPresetPayload(form);
    expect(payloadB.ok).toBe(true);
    if (!payloadB.ok) return;
    expect(payloadEquals(payloadA, payloadB.payload)).toBe(true);
  });

  it("maps profile template to form for fast switching", () => {
    const profile: LLMProfile = {
      id: "prof-1",
      owner_user_id: "u1",
      name: "模板A",
      provider: "openai_compatible",
      base_url: "https://api.example.com/v1",
      model: "x-model",
      temperature: 0.25,
      top_p: 0.95,
      max_tokens: 4096,
      presence_penalty: 0,
      frequency_penalty: 0.1,
      top_k: null,
      stop: ["END"],
      timeout_seconds: 222,
      extra: { reasoning_effort: "low" },
      has_api_key: true,
      masked_api_key: "sk-****1234",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    };
    const form = formFromProfile(profile);
    expect(form.provider).toBe("openai_compatible");
    expect(form.model).toBe("x-model");
    expect(form.timeout_seconds).toBe("222");
    expect(form.stop).toContain("END");
    expect(form.reasoning_effort).toBe("low");
  });
});
