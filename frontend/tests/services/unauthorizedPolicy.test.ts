import { describe, expect, it } from "vitest";

import { shouldNotifyUnauthorized } from "@/services/unauthorizedPolicy";

describe("shouldNotifyUnauthorized", () => {
  it("returns false for non-401 status", () => {
    expect(shouldNotifyUnauthorized(400, "UNAUTHORIZED")).toBe(false);
    expect(shouldNotifyUnauthorized(500, "UNAUTHORIZED")).toBe(false);
  });

  it("returns true for 401 + UNAUTHORIZED", () => {
    expect(shouldNotifyUnauthorized(401, "UNAUTHORIZED")).toBe(true);
    expect(shouldNotifyUnauthorized(401, " unauthorized ")).toBe(true);
  });

  it("returns false for 401 + llm error code", () => {
    expect(shouldNotifyUnauthorized(401, "LLM_AUTH_ERROR")).toBe(false);
    expect(shouldNotifyUnauthorized(401, "LLM_KEY_MISSING")).toBe(false);
  });

  it("keeps fail-safe behavior for unknown 401 payload", () => {
    expect(shouldNotifyUnauthorized(401, "")).toBe(true);
    expect(shouldNotifyUnauthorized(401, null)).toBe(true);
    expect(shouldNotifyUnauthorized(401, undefined)).toBe(true);
  });
});
