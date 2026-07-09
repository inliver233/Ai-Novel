import { describe, expect, it } from "vitest";

import { createRequestSeqGuard } from "@/lib/requestSeqGuard";

describe("createRequestSeqGuard", () => {
  it("treats only latest seq as valid", () => {
    const guard = createRequestSeqGuard();

    const seq1 = guard.next();
    expect(guard.isLatest(seq1)).toBe(true);

    const seq2 = guard.next();
    expect(guard.isLatest(seq1)).toBe(false);
    expect(guard.isLatest(seq2)).toBe(true);

    guard.invalidate();
    expect(guard.isLatest(seq2)).toBe(false);
  });
});
