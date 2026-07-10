import { describe, expect, it } from "vitest";

import { ApiError } from "@/services/apiClient";
import { formatLlmTestApiError } from "@/pages/prompts/llmApiError";

describe("prompts/llmApiError", () => {
  it("explains missing key errors with the saved-key contract", () => {
    const err = new ApiError({
      code: "LLM_KEY_MISSING",
      message: "missing",
      requestId: "req-1",
      status: 400,
    });
    const formatted = formatLlmTestApiError(err);
    expect(formatted).toEqual(expect.any(String));
    expect(formatted).not.toHaveLength(0);
    expect(formatted).not.toBe(err.message);
  });

  it("extracts upstream bad-request detail and compat adjustments", () => {
    const err = new ApiError({
      code: "LLM_BAD_REQUEST",
      message: "bad request",
      requestId: "req-2",
      status: 400,
      details: {
        upstream_error: JSON.stringify({
          error: {
            message: "upstream-detail-sentinel",
          },
        }),
        compat_adjustments: ["compat-one-sentinel", "compat-two-sentinel"],
      },
    });
    const formatted = formatLlmTestApiError(err);
    expect(formatted).toContain("upstream-detail-sentinel");
    expect(formatted).toContain("compat-one-sentinel");
    expect(formatted).toContain("compat-two-sentinel");
  });

  it("surfaces upstream status codes for transient service failures", () => {
    const err = new ApiError({
      code: "LLM_UPSTREAM_ERROR",
      message: "upstream error",
      requestId: "req-3",
      status: 502,
      details: {
        status_code: 503,
      },
    });
    const formatted = formatLlmTestApiError(err);
    expect(formatted).toContain("503");
    expect(formatted).not.toBe(err.message);
  });
});
