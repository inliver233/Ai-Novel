import { describe, expect, it } from "vitest";

import { createChapterMarkerStreamParser } from "@/services/chapterMarkerStreamParser";

function feedChunks(parser: ReturnType<typeof createChapterMarkerStreamParser>, chunks: string[]) {
  let content = "";
  let summary = "";
  let maxBuffer = 0;
  for (const chunk of chunks) {
    const out = parser.push(chunk);
    content += out.contentDelta;
    summary += out.summaryDelta;
    maxBuffer = Math.max(maxBuffer, parser.getBufferLength());
  }
  const end = parser.finalize();
  content += end.contentDelta;
  summary += end.summaryDelta;
  maxBuffer = Math.max(maxBuffer, parser.getBufferLength());
  return { content, summary, maxBuffer, phase: parser.getPhase() };
}

describe("createChapterMarkerStreamParser", () => {
  it("parses markers split across chunk boundaries", () => {
    const parser = createChapterMarkerStreamParser({ maxPreambleChars: 4096, tailKeepChars: 64 });
    const { content, summary, phase } = feedChunks(parser, [
      "<<<CO",
      "NTENT>>>\nHello content.\n<<<SU",
      "MMARY>>>\nHello summary.",
    ]);

    expect(phase).toBe("summary");
    expect(content).toBe("Hello content.\n");
    expect(summary).toBe("Hello summary.");
    expect(content).not.toContain("<<<");
    expect(summary).not.toContain("<<<");
  });

  it("tolerates whitespace/newline drift around markers", () => {
    const parser = createChapterMarkerStreamParser({ maxPreambleChars: 4096, tailKeepChars: 64 });
    const { content, summary } = feedChunks(parser, ["\n  <<<  CONTENT   >>>\nHello\n\n", "\t<<< SUMMARY>>>  \nSum"]);

    expect(content).toBe("Hello\n\n");
    expect(summary).toBe("Sum");
  });

  it("does not lose output when markers are absent (raw fallback)", () => {
    const parser = createChapterMarkerStreamParser({ maxPreambleChars: 16, tailKeepChars: 64 });
    const { content, summary, phase } = feedChunks(parser, ["Hello ", "world", "!"]);

    expect(phase).toBe("raw");
    expect(content).toBe("Hello world!");
    expect(summary).toBe("");
  });

  it("keeps internal buffer bounded on long streams", () => {
    const parser = createChapterMarkerStreamParser({ maxPreambleChars: 4096, tailKeepChars: 128 });
    const longContent = "A".repeat(100_000);
    const longSummary = "S".repeat(10_000);
    const full = `<<<CONTENT>>>\n${longContent}\n<<<SUMMARY>>>\n${longSummary}`;

    // Feed in uneven chunks to simulate streaming.
    const chunks: string[] = [];
    for (let i = 0; i < full.length; ) {
      const size = 1 + ((i * 7) % 97); // 1..97
      chunks.push(full.slice(i, i + size));
      i += size;
    }

    const { content, summary, maxBuffer, phase } = feedChunks(parser, chunks);

    expect(phase).toBe("summary");
    expect(content).toContain(longContent);
    expect(summary).toContain(longSummary);
    expect(content).not.toContain("<<<CONTENT");
    expect(content).not.toContain("<<<SUMMARY");
    expect(summary).not.toContain("<<<");
    expect(maxBuffer).toBeLessThanOrEqual(256);
  });
});
