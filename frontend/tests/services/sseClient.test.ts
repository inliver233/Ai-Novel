import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import { SSEPostClient } from "@/services/sseClient";
import { makeApiErrorResponse, makeSseResponse } from "../setup";

// SSEPostClient 是流式章节生成的核心传输（H43：此前零覆盖）。覆盖：完整回调序列
// 与累积、协议错误、HTTP 错误信封、流内 error 事件、提前断流。

describe("SSEPostClient.connect", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => vi.unstubAllGlobals());

  it("按 start→chunk→result→done 序列回调并累积内容", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      makeSseResponse(
        [
          { event: "start", data: { type: "start", message: "生成中", progress: 0, status: "processing" } },
          { event: "chunk", data: { type: "chunk", content: "Hello" } },
          { event: "chunk", data: { type: "chunk", content: " World" } },
          { event: "result", data: { type: "result", data: { id: "r1" } } },
          { event: "done", data: { type: "done" } },
        ],
        { headers: { "X-Request-Id": "rid-sse" } },
      ),
    );

    const calls: string[] = [];
    const client = new SSEPostClient("/api/x", { q: 1 }, {
      onOpen: (info) => calls.push(`open:${info.requestId}`),
      onProgress: (m) => calls.push(`progress:${m.progress}`),
      onChunk: (c) => calls.push(`chunk:${c}`),
      onResult: (d) => calls.push(`result:${(d as { id: string }).id}`),
      onDone: () => calls.push("done"),
    });

    const res = await client.connect();

    expect(calls).toEqual(["open:rid-sse", "progress:0", "chunk:Hello", "chunk: World", "result:r1", "done"]);
    expect(res.accumulatedContent).toBe("Hello World");
    expect(res.requestId).toBe("rid-sse");
    expect((res.result as { id: string }).id).toBe("r1");
  });

  it("非 event-stream 响应抛 SSE_PROTOCOL_ERROR", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    const client = new SSEPostClient("/api/x", {}, {});
    await expect(client.connect()).rejects.toMatchObject({ name: "SSEError", code: "SSE_PROTOCOL_ERROR" });
  });

  it("HTTP 错误信封抛 ApiError（status/requestId 透传）", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(makeApiErrorResponse("VALIDATION_ERROR", "bad", 400, "rid-e"));
    const client = new SSEPostClient("/api/x", {}, {});
    await expect(client.connect()).rejects.toMatchObject({
      name: "ApiError",
      code: "VALIDATION_ERROR",
      status: 400,
      requestId: "rid-e",
    });
  });

  it("流内 error 事件触发 onError 并抛 SSE_SERVER_ERROR", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      makeSseResponse([
        { event: "chunk", data: { type: "chunk", content: "partial" } },
        { event: "error", data: { type: "error", error: "模型超时", code: 504 } },
      ]),
    );
    let errMsg: string | undefined;
    let errCode: number | undefined;
    const client = new SSEPostClient("/api/x", {}, {
      onError: (e, c) => {
        errMsg = e;
        errCode = c;
      },
    });
    await expect(client.connect()).rejects.toMatchObject({ name: "SSEError", code: "SSE_SERVER_ERROR" });
    expect(errMsg).toBe("模型超时");
    expect(errCode).toBe(504);
  });

  it("连接阶段网络失败抛 SSE_CONNECTION_ERROR（非 abort）", async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new TypeError("network down"));
    const client = new SSEPostClient("/api/x", {}, {});
    await expect(client.connect()).rejects.toMatchObject({ name: "SSEError", code: "SSE_CONNECTION_ERROR" });
  });

  it("有 result 但缺 done 的提前断流仍正常返回（兼容代理丢终止事件）", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      makeSseResponse([
        { event: "chunk", data: { type: "chunk", content: "X" } },
        { event: "result", data: { type: "result", data: { ok: 1 } } },
        // 故意不发 done
      ]),
    );
    let doneCalled = false;
    const client = new SSEPostClient("/api/x", {}, { onDone: () => (doneCalled = true) });
    const res = await client.connect();
    expect(res.accumulatedContent).toBe("X");
    expect(doneCalled).toBe(true);
  });

  it("无 result 无 done 的提前断流抛 SSE_EARLY_CLOSE", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      makeSseResponse([{ event: "chunk", data: { type: "chunk", content: "Y" } }]),
    );
    const client = new SSEPostClient("/api/x", {}, {});
    await expect(client.connect()).rejects.toMatchObject({ name: "SSEError", code: "SSE_EARLY_CLOSE" });
  });
});

describe("SSEPostClient.abort", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => vi.unstubAllGlobals());

  it("abort 后连接抛 ABORTED", async () => {
    vi.mocked(fetch).mockImplementationOnce(
      (_input, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        }),
    );
    const client = new SSEPostClient("/api/x", {}, {});
    const p = client.connect();
    client.abort();
    await expect(p).rejects.toMatchObject({ name: "SSEError", code: "ABORTED" });
  });
});
