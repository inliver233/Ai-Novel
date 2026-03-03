import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ProgressBar } from "./ProgressBar";

describe("ProgressBar", () => {
  it("renders progressbar aria attributes with clamped values", () => {
    const html = renderToStaticMarkup(<ProgressBar value={120} ariaLabel="测试进度" />);

    expect(html).toContain('role="progressbar"');
    expect(html).toContain('aria-label="测试进度"');
    expect(html).toContain('aria-valuemin="0"');
    expect(html).toContain('aria-valuemax="100"');
    expect(html).toContain('aria-valuenow="100"');
    expect(html).toContain("width:100%");
  });

  it("supports custom min/max range", () => {
    const html = renderToStaticMarkup(<ProgressBar value={6} min={0} max={8} ariaLabel="范围进度" />);

    expect(html).toContain('aria-valuemax="8"');
    expect(html).toContain('aria-valuenow="6"');
    expect(html).toContain("width:75%");
  });
});
