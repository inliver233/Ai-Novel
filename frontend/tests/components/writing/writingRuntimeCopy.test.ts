import { describe, expect, it } from "vitest";

import { formatWritingDisabledReason } from "@/components/writing/writingRuntimeCopy";

describe("writingRuntimeCopy", () => {
  it("preserves explicit disabled reasons and normalizes missing reasons", () => {
    const explicitReason = formatWritingDisabledReason("reason-sentinel");
    const missingReason = formatWritingDisabledReason(null);

    expect(explicitReason).toContain("reason-sentinel");
    expect(explicitReason).not.toContain("undefined");
    expect(missingReason).not.toContain("null");
    expect(missingReason).not.toContain("undefined");
    expect(missingReason).not.toBe(explicitReason);
  });
});
