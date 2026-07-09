import { describe, expect, it } from "vitest";

import { computeNextAuthRefreshDelayMs } from "@/contexts/auth";

describe("computeNextAuthRefreshDelayMs", () => {
  it("returns default delay when expireAtSec is null", () => {
    expect(computeNextAuthRefreshDelayMs({ expireAtSec: null, nowMs: 0 })).toBe(5 * 60_000);
  });

  it("schedules refresh based on expireAtSec (far future)", () => {
    expect(computeNextAuthRefreshDelayMs({ expireAtSec: 3600, nowMs: 0 })).toBe(3_240_000);
  });

  it("clamps to min delay when expireAtSec is near", () => {
    expect(computeNextAuthRefreshDelayMs({ expireAtSec: 120, nowMs: 0 })).toBe(30_000);
  });

  it("clamps to min delay when expireAtSec is in the past", () => {
    expect(computeNextAuthRefreshDelayMs({ expireAtSec: 30, nowMs: 0 })).toBe(30_000);
  });

  it("returns default delay for non-finite expireAtSec", () => {
    expect(computeNextAuthRefreshDelayMs({ expireAtSec: Number.NaN, nowMs: 0 })).toBe(5 * 60_000);
    expect(computeNextAuthRefreshDelayMs({ expireAtSec: Number.POSITIVE_INFINITY, nowMs: 0 })).toBe(5 * 60_000);
  });

  it("caps extremely large delay to avoid setTimeout overflow", () => {
    expect(computeNextAuthRefreshDelayMs({ expireAtSec: 10_000_000_000, nowMs: 0 })).toBe(2_000_000_000);
  });
});
