import { describe, expect, it } from "vitest";

import { ApiError } from "@/services/apiClient";
import { toApiError } from "@/services/apiError";

describe("toApiError", () => {
  it("preserves ApiError identity", () => {
    const error = new ApiError({ code: "FAILED", message: "失败", requestId: "rid-1", status: 409 });
    expect(toApiError(error)).toBe(error);
  });

  it.each([
    [new Error("原始错误"), "原始错误"],
    ["字符串错误", "字符串错误"],
    [{ reason: "object" }, "请求失败"],
    [null, "请求失败"],
  ])("normalizes unknown thrown values safely", (thrown, expectedMessage) => {
    const error = toApiError(thrown);
    expect(error).toMatchObject({ code: "UNKNOWN", message: expectedMessage, requestId: "unknown", status: 0 });
    expect(error.details).toBe(thrown);
  });

  it("supports stable caller fallbacks", () => {
    expect(toApiError(null, { code: "LOAD_FAILED", message: "加载失败" })).toMatchObject({
      code: "LOAD_FAILED",
      message: "加载失败",
      requestId: "unknown",
      status: 0,
    });
    expect(toApiError(new Error("具体错误"), { message: "加载失败" }).message).toBe("加载失败");
  });
});
