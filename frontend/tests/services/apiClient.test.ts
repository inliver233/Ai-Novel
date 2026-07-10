import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import { ApiError, apiJson, sanitizeFilename, type ApiOkPayload } from "@/services/apiClient";
import { makeApiErrorResponse, makeJsonResponse } from "../setup";

// apiClient 是前端唯一的传输层入口（H43：此前零覆盖）。覆盖：成功解包、
// 错误信封→ApiError、超时、外部取消、网络错误、401 未授权事件总线。

describe("apiJson", () => {
  beforeEach(() => {
    // node 环境无全局 fetch，逐个用例打桩。
    vi.stubGlobal("fetch", vi.fn());
  });

  it("解包成功响应的 data", async () => {
    const body: ApiOkPayload<{ id: string }> = { ok: true, data: { id: "c1" }, request_id: "rid-1" };
    vi.mocked(fetch).mockResolvedValueOnce(makeJsonResponse(body, { headers: { "X-Request-Id": "rid-1" } }));

    const res = await apiJson<{ id: string }>("/api/projects/p1/chapters/c1");

    expect(fetch).toHaveBeenCalledWith(
      "/api/projects/p1/chapters/c1",
      expect.objectContaining({
        credentials: "include",
        headers: expect.objectContaining({ "Content-Type": "application/json" }),
      }),
    );
    expect(res.ok).toBe(true);
    expect(res.data.id).toBe("c1");
    expect(res.request_id).toBe("rid-1");
  });

  it("把错误信封转为 ApiError 并保留 code/requestId/status", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(makeApiErrorResponse("VALIDATION_ERROR", "参数错误", 400, "rid-err"));

    await expect(apiJson("/api/x")).rejects.toMatchObject({
      name: "ApiError",
      code: "VALIDATION_ERROR",
      requestId: "rid-err",
      status: 400,
    });
  });

  it("超时抛出 TIMEOUT（而非裸 AbortError）", async () => {
    vi.useFakeTimers();
    try {
      // fetch 永不 resolve，直到 abort 触发 rejection。
      vi.mocked(fetch).mockImplementationOnce(
        (_input, init) =>
          new Promise((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
          }),
      );
      const p = apiJson("/api/slow", { timeoutMs: 50 });
      // 先挂 reject 处理器，避免 advance timer 触发 rejection 时形成未捕获拒绝。
      const assertion = expect(p).rejects.toMatchObject({ name: "ApiError", code: "TIMEOUT", status: 0 });
      await vi.advanceTimersByTimeAsync(60);
      await assertion;
    } finally {
      vi.useRealTimers();
    }
  });

  it("网络错误抛出 NETWORK_ERROR", async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new TypeError("fetch failed"));

    await expect(apiJson("/api/x")).rejects.toMatchObject({ name: "ApiError", code: "NETWORK_ERROR", status: 0 });
  });

  it("401 + UNAUTHORIZED 错误码派发 ainovel:unauthorized 事件", async () => {
    // 用真实 EventTarget 作为 window，使 dispatchEvent 可被监听验证。
    const target = new EventTarget();
    const dispatchSpy = vi.spyOn(target, "dispatchEvent");
    vi.stubGlobal("window", target);
    vi.stubGlobal("CustomEvent", CustomEvent);
    let unauthorizedFired = false;
    target.addEventListener("ainovel:unauthorized", () => (unauthorizedFired = true));

    vi.mocked(fetch).mockResolvedValueOnce(makeApiErrorResponse("UNAUTHORIZED", "未登录", 401, "rid-401"));

    await expect(apiJson("/api/x")).rejects.toBeInstanceOf(ApiError);
    expect(dispatchSpy).toHaveBeenCalled();
    expect(unauthorizedFired).toBe(true);
  });

  it("非 ok 信封且非对象响应抛出 BAD_RESPONSE", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response("not json at all", { status: 502 }));

    await expect(apiJson("/api/x")).rejects.toMatchObject({ name: "ApiError", code: "BAD_RESPONSE", status: 502 });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });
});

describe("sanitizeFilename", () => {
  it("取末段路径并替换非法字符（防目录穿越）", () => {
    // split(/[/\\]/).pop() 只保留最后一段：a/b → b
    expect(sanitizeFilename("a/b\\c:d?e")).toBe("c_d_e");
    expect(sanitizeFilename("dir/sub/file.txt")).toBe("file.txt");
  });

  it("截断超长名（≤80）", () => {
    expect(sanitizeFilename("x".repeat(120)).length).toBe(80);
  });

  it("空串返回空", () => {
    expect(sanitizeFilename("   ")).toBe("");
  });
});
