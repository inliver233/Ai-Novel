import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import { SSEPostClient } from "@/services/sseClient";
import {
  createChapterMarkerStreamParser,
  type ChapterMarkerStreamPhase,
} from "@/services/chapterMarkerStreamParser";
import { makeSseResponse } from "../setup";

// 集成层（D 类 happy-path，应绿）：复刻写作页 SSE 流式生成主链路两端的真实协作。
// - SSEPostClient 消费 text/event-stream：按 "\n\n" 切块、提取 event/data 行、
//   JSON 解析后按 type（或 event 名）派发回调（start/progress/chunk/result/done），
//   并把 chunk/token 的 content 累积为 accumulatedContent。
// - chapterMarkerStreamParser 接收 onChunk 的每个 token 增量，按 <<<CONTENT>>>/
//   <<<SUMMARY>>> 标记把裸文本切分为正文/摘要，内部维持有界缓冲。
// 单测分别覆盖两端（chapterMarkerStreamParser.test.ts / sseClient.test.ts）；
// 此处覆盖二者端到端协作：事件序列→标记重组、容错跳过、完成态冲刷。

type StreamOutcome = {
  content: string;
  summary: string;
  phase: ChapterMarkerStreamPhase;
  progressMessages: string[];
  doneCalled: boolean;
  accumulatedContent: string;
  result: unknown;
  requestId?: string;
};

// 把一个 SSE Response 喂给「SSEPostClient.onChunk → parser.push」主链路，
// 流结束后 finalize 冲刷残量，返回解析后的正文/摘要/状态（与写作页 onDone 后 finalize 对齐）。
async function streamChapter(response: Response): Promise<StreamOutcome> {
  vi.mocked(fetch).mockResolvedValueOnce(response);

  const parser = createChapterMarkerStreamParser();
  let content = "";
  let summary = "";
  const progressMessages: string[] = [];
  let doneCalled = false;

  const client = new SSEPostClient("/api/chapter/generate", { prompt: "x" }, {
    onChunk: (c) => {
      const out = parser.push(c);
      content += out.contentDelta;
      summary += out.summaryDelta;
    },
    onProgress: (m) => {
      progressMessages.push(m.message);
    },
    onDone: () => {
      doneCalled = true;
    },
  });

  const res = await client.connect();
  const end = parser.finalize();
  content += end.contentDelta;
  summary += end.summaryDelta;

  return {
    content,
    summary,
    phase: parser.getPhase(),
    progressMessages,
    doneCalled,
    accumulatedContent: res.accumulatedContent,
    result: res.result,
    requestId: res.requestId,
  };
}

// 构造含非法 JSON data 行的原始 SSE 响应。makeSseResponse 恒产出合法 JSON，
// 故此处手工拼装，以覆盖 sseClient 的 JSON.parse 失败容错分支（源码 try/catch → continue）。
function makeRawSseResponse(rawBody: string): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(rawBody));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

describe("SSE chapter marker stream integration", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => vi.unstubAllGlobals());

  it("start→chunks→progress→result→done 全链路解析为正文与摘要", async () => {
    const out = await streamChapter(
      makeSseResponse(
        [
          { event: "start", data: { type: "start", message: "generating", progress: 0, status: "processing" } },
          { event: "chunk", data: { type: "chunk", content: "<<<CONTENT>>>\n" } },
          { event: "chunk", data: { type: "chunk", content: "Paragraph one. " } },
          { event: "chunk", data: { type: "chunk", content: "Paragraph two.\n" } },
          { event: "progress", data: { type: "progress", message: "halfway", progress: 50, status: "processing" } },
          { event: "chunk", data: { type: "chunk", content: "<<<SUMMARY>>>\n" } },
          { event: "chunk", data: { type: "chunk", content: "Chapter summary." } },
          { event: "result", data: { type: "result", data: { chapter_id: "c1" } } },
          { event: "done", data: { type: "done" } },
        ],
        { headers: { "X-Request-Id": "rid-int-1" } },
      ),
    );

    expect(out.phase).toBe("summary");
    expect(out.content).toBe("Paragraph one. Paragraph two.\n");
    expect(out.summary).toBe("Chapter summary.");
    expect(out.doneCalled).toBe(true);
    expect(out.progressMessages).toEqual(["generating", "halfway"]);
    expect((out.result as { chapter_id: string }).chapter_id).toBe("c1");
    expect(out.requestId).toBe("rid-int-1");
    // 累积正文是标记去除前的原始流，应同时包含两端标记。
    expect(out.accumulatedContent).toContain("<<<CONTENT>>>");
    expect(out.accumulatedContent).toContain("<<<SUMMARY>>>");
  });

  it("标记跨多个 chunk 边界拆分仍正确重组正文与摘要", async () => {
    const out = await streamChapter(
      makeSseResponse([
        { event: "chunk", data: { type: "chunk", content: "<<<CO" } },
        { event: "chunk", data: { type: "chunk", content: "NTENT>>>\nFragmented " } },
        { event: "chunk", data: { type: "chunk", content: "stream " } },
        { event: "chunk", data: { type: "chunk", content: "body.\n<<<SU" } },
        { event: "chunk", data: { type: "chunk", content: "MMARY>>>\nFragmented " } },
        { event: "chunk", data: { type: "chunk", content: "summary." } },
        { event: "done", data: { type: "done" } },
      ]),
    );

    expect(out.phase).toBe("summary");
    expect(out.content).toBe("Fragmented stream body.\n");
    expect(out.summary).toBe("Fragmented summary.");
    expect(out.doneCalled).toBe(true);
  });

  it("无标记的裸文本触发 raw 回退，全部作为正文输出", async () => {
    const out = await streamChapter(
      makeSseResponse([
        { event: "chunk", data: { type: "chunk", content: "Raw text " } },
        { event: "chunk", data: { type: "chunk", content: "without any markers." } },
        { event: "done", data: { type: "done" } },
      ]),
    );

    expect(out.phase).toBe("raw");
    expect(out.content).toBe("Raw text without any markers.");
    expect(out.summary).toBe("");
  });

  it("空内容流（start→done 无 chunk）保持 before 态且正文为空", async () => {
    const out = await streamChapter(
      makeSseResponse([
        { event: "start", data: { type: "start", message: "noop", progress: 0, status: "processing" } },
        { event: "done", data: { type: "done" } },
      ]),
    );

    expect(out.phase).toBe("before");
    expect(out.content).toBe("");
    expect(out.summary).toBe("");
    expect(out.doneCalled).toBe(true);
  });

  it("缺 content 字段的 chunk 事件被容错跳过，有效 chunk 仍流入解析", async () => {
    const out = await streamChapter(
      makeSseResponse([
        { event: "chunk", data: { type: "chunk", content: "Valid " } },
        // 无 content 字段：sseClient 判 typeof obj.content !== "string" → continue 跳过
        { event: "chunk", data: { type: "chunk" } },
        { event: "chunk", data: { type: "chunk", content: "stream." } },
        { event: "done", data: { type: "done" } },
      ]),
    );

    expect(out.phase).toBe("raw");
    // 累积值（SSE 层）与解析值（parser 层）都只含两个有效 chunk
    expect(out.accumulatedContent).toBe("Valid stream.");
    expect(out.content).toBe("Valid stream.");
  });

  it("JSON 解析失败的 data 块被容错跳过，不污染有效块", async () => {
    const rawBody =
      [
        `data: ${JSON.stringify({ type: "chunk", content: "Before " })}`,
        `data: {not valid json`,
        `data: ${JSON.stringify({ type: "chunk", content: "After" })}`,
        `data: ${JSON.stringify({ type: "done" })}`,
      ].join("\n\n") + "\n\n";

    const out = await streamChapter(makeRawSseResponse(rawBody));

    expect(out.phase).toBe("raw");
    expect(out.accumulatedContent).toBe("Before After");
    expect(out.content).toBe("Before After");
    expect(out.doneCalled).toBe(true);
  });

  it("仅有 data 行无 event 行时，按 payload type 字段正确派发", async () => {
    const out = await streamChapter(
      makeSseResponse([
        // 不带 event 字段：sseClient 用 data.type 作为 eventType 派发
        { data: { type: "start", message: "data-only", progress: 0, status: "processing" } },
        { data: { type: "chunk", content: "<<<CONTENT>>>\nData-only " } },
        { data: { type: "chunk", content: "block.\n<<<SUMMARY>>>\nData summary." } },
        { data: { type: "done" } },
      ]),
    );

    expect(out.phase).toBe("summary");
    expect(out.content).toBe("Data-only block.\n");
    expect(out.summary).toBe("Data summary.");
    expect(out.doneCalled).toBe(true);
    expect(out.progressMessages).toEqual(["data-only"]);
  });
});
