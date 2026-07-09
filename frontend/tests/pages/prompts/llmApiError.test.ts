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
    expect(formatLlmTestApiError(err)).toBe("请先保存 API Key");
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
            message: "unsupported response_format",
          },
        }),
        compat_adjustments: ["lowered max_tokens", "removed top_p"],
      },
    });
    expect(formatLlmTestApiError(err)).toBe(
      "请求参数有误，可能是模型名称或参数不支持（上游：unsupported response_format）（兼容：lowered max_tokens、removed top_p）",
    );
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
    expect(formatLlmTestApiError(err)).toBe("服务暂时不可用，请稍后重试（503）");
  });
});
