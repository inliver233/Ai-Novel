/**
 * vitest 全局 setup（node 与 jsdom 环境共用）。
 *
 * - 注册 @testing-library/jest-dom 的 DOM 断言匹配器（toBeInTheDocument 等），
 *   在 node 环境下导入是安全的（仅扩展 expect，不触碰 DOM）。
 * - 提供 fetch / ReadableStream 的轻量 mock 工具，供 apiClient / sseClient 测试复用，
 *   避免每个测试各自手搓 Response（见 项目情况完全分析.md H43/M41）。
 */
import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import type { ApiErrorPayload } from "@/services/apiClient";

// 每个 DOM/交互测试之间清理 DOM，避免跨用例污染。
afterEach(() => {
  if (typeof document !== "undefined") {
    document.body.innerHTML = "";
  }
  vi.restoreAllMocks();
});

/**
 * 构造一个最小的 fetch Response（非流式 JSON）。
 * 用于 apiClient 的成功/错误路径测试。
 */
export function makeJsonResponse(
  body: unknown,
  init: { status?: number; headers?: Record<string, string> } = {},
): Response {
  const status = init.status ?? 200;
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
}

/**
 * 构造一个后端错误信封响应（{ok:false,error,request_id}）。
 */
export function makeApiErrorResponse(code: string, message: string, status = 400, requestId = "rid-test"): Response {
  const body: ApiErrorPayload = { ok: false, error: { code, message }, request_id: requestId };
  return makeJsonResponse(body, { status });
}

/**
 * 构造一个 SSE text/event-stream Response，body 由若干 [event,data] 块组成。
 * 用于 sseClient（SSEPostClient）测试。每块以 "\n\n" 分隔，data 行为 JSON 字符串。
 */
export function makeSseResponse(
  blocks: Array<{ event?: string; data: unknown }>,
  init: { status?: number; headers?: Record<string, string> } = {},
): Response {
  const chunks: string[] = [];
  for (const block of blocks) {
    const parts: string[] = [];
    if (block.event) parts.push(`event: ${block.event}`);
    parts.push(`data: ${JSON.stringify(block.data)}`);
    chunks.push(parts.join("\n") + "\n\n");
  }
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(stream, {
    status: init.status ?? 200,
    headers: { "Content-Type": "text/event-stream", ...(init.headers ?? {}) },
  });
}
