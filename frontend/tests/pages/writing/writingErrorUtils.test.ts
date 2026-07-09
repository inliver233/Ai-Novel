import { describe, expect, it } from "vitest";

import { ApiError } from "@/services/apiClient";
import { extractMissingNumbers } from "@/pages/writing/writingErrorUtils";

describe("extractMissingNumbers", () => {
  it("returns [] for non-ApiError values", () => {
    expect(extractMissingNumbers("nope")).toEqual([]);
    expect(extractMissingNumbers({})).toEqual([]);
  });

  it("returns [] for ApiError with other code", () => {
    const err = new ApiError({ code: "OTHER", message: "x", requestId: "r", status: 400, details: {} });
    expect(extractMissingNumbers(err)).toEqual([]);
  });

  it("extracts missing_numbers from ApiError.details", () => {
    const err = new ApiError({
      code: "CHAPTER_PREREQ_MISSING",
      message: "x",
      requestId: "r",
      status: 400,
      details: { missing_numbers: [1, 2, 3] },
    });
    expect(extractMissingNumbers(err)).toEqual([1, 2, 3]);
  });

  it("filters non-number items", () => {
    const err = new ApiError({
      code: "CHAPTER_PREREQ_MISSING",
      message: "x",
      requestId: "r",
      status: 400,
      details: { missing_numbers: [1, "2", null, 3] },
    });
    expect(extractMissingNumbers(err)).toEqual([1, 3]);
  });

  it("returns [] when details schema is unexpected", () => {
    const err1 = new ApiError({
      code: "CHAPTER_PREREQ_MISSING",
      message: "x",
      requestId: "r",
      status: 400,
      details: { missing_numbers: "nope" },
    });
    const err2 = new ApiError({
      code: "CHAPTER_PREREQ_MISSING",
      message: "x",
      requestId: "r",
      status: 400,
      details: null,
    });
    expect(extractMissingNumbers(err1)).toEqual([]);
    expect(extractMissingNumbers(err2)).toEqual([]);
  });
});
