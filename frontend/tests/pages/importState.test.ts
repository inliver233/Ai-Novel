import { describe, expect, it } from "vitest";

import { getImportProposalDisabledReason, mergeImportDocuments, type ImportDocument } from "@/pages/importState";

function buildDoc(overrides: Partial<ImportDocument>): ImportDocument {
  return {
    id: "doc-1",
    project_id: "p1",
    actor_user_id: "u1",
    filename: "demo.txt",
    content_type: "txt",
    status: "queued",
    progress: 0,
    progress_message: "queued",
    chunk_count: 0,
    kb_id: null,
    error_message: null,
    created_at: "2026-03-13T00:00:00Z",
    updated_at: "2026-03-13T00:00:00Z",
    ...overrides,
  };
}

describe("importState", () => {
  it("keeps optimistic documents when an older list response is empty", () => {
    const optimistic = buildDoc({ id: "doc-new", filename: "new.txt", updated_at: "2026-03-13T00:00:05Z" });
    const merged = mergeImportDocuments([optimistic], []);
    expect(merged).toHaveLength(1);
    expect(merged[0].id).toBe("doc-new");
  });

  it("prefers the newer detail status for the same document", () => {
    const running = buildDoc({ status: "running", progress: 25, updated_at: "2026-03-13T00:00:01Z" });
    const done = buildDoc({ status: "done", progress: 100, updated_at: "2026-03-13T00:00:03Z" });
    const merged = mergeImportDocuments([running], [done]);
    expect(merged[0].status).toBe("done");
    expect(merged[0].progress).toBe(100);
  });

  it("returns a disabled reason until import is done", () => {
    const queuedReason = getImportProposalDisabledReason("queued");
    const runningReason = getImportProposalDisabledReason("running");
    const failedReason = getImportProposalDisabledReason("failed");
    const unknownReason = getImportProposalDisabledReason("status-sentinel");

    expect(queuedReason).toEqual(expect.any(String));
    expect(runningReason).toBe(queuedReason);
    expect(failedReason).toEqual(expect.any(String));
    expect(failedReason).not.toBe(queuedReason);
    expect(unknownReason).toContain("status-sentinel");
    expect(getImportProposalDisabledReason("done")).toBeNull();
  });
});
