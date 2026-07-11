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
    const error = makeError("rid-toast");
    expect(toastApiError({ toastError }, error)).toBe(error);
    expect(toastError).toHaveBeenCalledWith("操作失败 (FAILED)", "rid-toast");
  });

  it("安全归一化 unknown 并返回统一错误", () => {
    const toastError = vi.fn();
    const normalized = toastApiError({ toastError }, null);
    expect(normalized).toMatchObject({ code: "UNKNOWN", message: "请求失败", requestId: "unknown", status: 0 });
    expect(toastError).toHaveBeenCalledWith("请求失败 (UNKNOWN)", undefined);
  });

  it("保留 SSE 等结构化 Error 的调用方 code 和 request id", () => {
    const toastError = vi.fn();
    const normalized = toastApiError({ toastError }, new Error("流式请求失败"), {
      code: "SSE_STREAM_ERROR",
      requestId: "rid-sse",
    });
    expect(normalized).toMatchObject({
      code: "SSE_STREAM_ERROR",
      message: "流式请求失败",
      requestId: "rid-sse",
      status: 0,
    });
    expect(toastError).toHaveBeenCalledWith("流式请求失败 (SSE_STREAM_ERROR)", "rid-sse");
  });
});
