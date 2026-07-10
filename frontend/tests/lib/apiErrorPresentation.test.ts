import { describe, expect, it, vi } from "vitest";

import { formatApiError, getApiErrorRequestId, toastApiError } from "@/lib/apiErrorPresentation";
import { ApiError } from "@/services/apiClient";

function makeError(requestId = "rid-1") {
  return new ApiError({ code: "FAILED", message: "操作失败", requestId, status: 500 });
}

describe("apiErrorPresentation", () => {
  it("统一格式化 ApiError 文案", () => {
    expect(formatApiError(makeError())).toBe("操作失败 (FAILED)");
  });

  it("隐藏 unknown request id", () => {
    expect(getApiErrorRequestId(makeError("unknown"))).toBeUndefined();
    expect(getApiErrorRequestId(makeError("rid-visible"))).toBe("rid-visible");
  });

  it("通过 toastError 展示统一文案和可追踪 request id", () => {
    const toastError = vi.fn();
    toastApiError({ toastError }, makeError("rid-toast"));
    expect(toastError).toHaveBeenCalledWith("操作失败 (FAILED)", "rid-toast");
  });
});
