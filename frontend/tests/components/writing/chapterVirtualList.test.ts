import { describe, expect, it } from "vitest";

import { getChapterScrollTopForIndex, getChapterVirtualWindow } from "@/components/writing/chapterVirtualWindow";

describe("ChapterVirtualList helpers", () => {
  it("computes a bounded virtual window with overscan", () => {
    const result = getChapterVirtualWindow({
      itemCount: 180,
      itemHeight: 48,
      viewportHeight: 240,
      scrollTop: 480,
      overscan: 2,
    });

    expect(result).toEqual({
      startIndex: 8,
      endIndex: 17,
      totalHeight: 8640,
      offsetTop: 384,
    });
  });

  it("clamps scroll ranges when the list shrinks", () => {
    expect(
      getChapterVirtualWindow({
        itemCount: 3,
        itemHeight: 48,
        viewportHeight: 240,
        scrollTop: 9_999,
      }),
    ).toEqual({
      startIndex: 0,
      endIndex: 3,
      totalHeight: 144,
      offsetTop: 0,
    });
  });

  it("clamps an empty virtual window", () => {
    expect(
      getChapterVirtualWindow({
        itemCount: 0,
        itemHeight: 48,
        viewportHeight: 240,
        scrollTop: 0,
      }),
    ).toEqual({
      startIndex: 0,
      endIndex: 0,
      totalHeight: 0,
      offsetTop: 0,
    });
  });

  it("returns null when the active chapter is already visible", () => {
    expect(
      getChapterScrollTopForIndex({
        currentScrollTop: 480,
        itemIndex: 11,
        itemHeight: 48,
        viewportHeight: 240,
      }),
    ).toBeNull();
  });

  it("scrolls to reveal chapters above and below the viewport", () => {
    expect(
      getChapterScrollTopForIndex({
        currentScrollTop: 480,
        itemIndex: 6,
        itemHeight: 48,
        viewportHeight: 240,
      }),
    ).toBe(288);

    expect(
      getChapterScrollTopForIndex({
        currentScrollTop: 480,
        itemIndex: 16,
        itemHeight: 48,
        viewportHeight: 240,
      }),
    ).toBe(576);
  });
});
