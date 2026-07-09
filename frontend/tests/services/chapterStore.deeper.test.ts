import { describe, expect, it, vi } from "vitest";

import { ApiError } from "@/services/apiClient";
import { createChapterStore } from "@/services/chapterStore";
import type {
  BulkCreateChapterInput,
  ChapterDetail,
  ChapterListItem,
  CreateChapterInput,
  UpdateChapterInput,
} from "@/types";

function makeDetail(overrides: Partial<ChapterDetail> = {}): ChapterDetail {
  return {
    id: "chapter-1",
    project_id: "project-1",
    outline_id: "outline-1",
    number: 1,
    title: "Chapter 1",
    plan: null,
    content_md: null,
    summary: null,
    status: "planned",
    updated_at: "2026-03-06T23:31:12Z",
    ...overrides,
  };
}

function makeListItem(overrides: Partial<ChapterListItem> = {}): ChapterListItem {
  const detail = makeDetail(overrides);
  return {
    id: detail.id,
    project_id: detail.project_id,
    outline_id: detail.outline_id,
    number: detail.number,
    title: detail.title,
    status: detail.status,
    updated_at: detail.updated_at,
    has_plan: Boolean(detail.plan),
    has_summary: Boolean(detail.summary),
    has_content: Boolean(detail.content_md),
    ...overrides,
  };
}

function buildTransport(
  args: {
    bulkCreateChapters?: (
      projectId: string,
      payload: BulkCreateChapterInput,
      options?: { replace?: boolean },
    ) => Promise<ChapterDetail[]>;
    createChapter?: (projectId: string, payload: CreateChapterInput) => Promise<ChapterDetail>;
    deleteChapter?: (chapterId: string) => Promise<void>;
    fetchAllChapterMeta?: (projectId: string) => Promise<ChapterListItem[]>;
    fetchChapterDetail?: (chapterId: string) => Promise<ChapterDetail>;
    updateChapter?: (chapterId: string, payload: UpdateChapterInput) => Promise<ChapterDetail>;
  } = {},
) {
  return {
    bulkCreateChapters: args.bulkCreateChapters ?? vi.fn(async () => []),
    createChapter: args.createChapter ?? vi.fn(async () => makeDetail()),
    deleteChapter: args.deleteChapter ?? vi.fn(async () => undefined),
    fetchAllChapterMeta: args.fetchAllChapterMeta ?? vi.fn(async () => []),
    fetchChapterDetail: args.fetchChapterDetail ?? vi.fn(async () => makeDetail()),
    updateChapter: args.updateChapter ?? vi.fn(async () => makeDetail()),
  };
}

describe("chapterStore (deeper interactions)", () => {
  it("fans out meta state changes to every active subscriber", async () => {
    const store = createChapterStore(
      buildTransport({ fetchAllChapterMeta: async () => [makeListItem()] }),
    );
    const listenerA = vi.fn();
    const listenerB = vi.fn();
    const unsubscribeA = store.subscribeMeta("project-1", listenerA);
    store.subscribeMeta("project-1", listenerB);

    await store.loadProjectChapterMeta("project-1");

    expect(listenerA).toHaveBeenCalled();
    expect(listenerB).toHaveBeenCalled();
    const callsA = listenerA.mock.calls.length;
    const callsB = listenerB.mock.calls.length;
    expect(callsA).toBeGreaterThan(0);
    expect(callsB).toBe(callsA);

    // Detaching one subscriber must silence only that one; the other keeps receiving.
    unsubscribeA();
    store.invalidateProjectChapters("project-1");

    expect(listenerA.mock.calls.length).toBe(callsA);
    expect(listenerB.mock.calls.length).toBeGreaterThan(callsB);
  });

  it("fans out detail state changes to every active subscriber", async () => {
    const store = createChapterStore(
      buildTransport({ fetchChapterDetail: async () => makeDetail() }),
    );
    const listenerA = vi.fn();
    const listenerB = vi.fn();
    const unsubscribeA = store.subscribeDetail("chapter-1", listenerA);
    store.subscribeDetail("chapter-1", listenerB);

    await store.loadChapterDetail("chapter-1");

    expect(listenerA).toHaveBeenCalled();
    expect(listenerB).toHaveBeenCalled();
    const callsA = listenerA.mock.calls.length;
    const callsB = listenerB.mock.calls.length;
    expect(callsA).toBeGreaterThan(0);
    expect(callsB).toBe(callsA);

    unsubscribeA();
    store.invalidateChapterDetail("chapter-1");

    expect(listenerA.mock.calls.length).toBe(callsA);
    expect(listenerB.mock.calls.length).toBeGreaterThan(callsB);
  });

  it("refetches detail after invalidateChapterDetail marks it stale", async () => {
    const fetchChapterDetail = vi.fn(async () => makeDetail());
    const store = createChapterStore(buildTransport({ fetchChapterDetail }));

    await store.loadChapterDetail("chapter-1");
    expect(fetchChapterDetail).toHaveBeenCalledTimes(1);
    expect(store.getDetailSnapshot("chapter-1").stale).toBe(false);

    store.invalidateChapterDetail("chapter-1");
    expect(store.getDetailSnapshot("chapter-1").stale).toBe(true);

    // Stale bypasses the cache check even without { force: true }, so the transport fires again.
    await store.loadChapterDetail("chapter-1");
    expect(fetchChapterDetail).toHaveBeenCalledTimes(2);
    expect(store.getDetailSnapshot("chapter-1").stale).toBe(false);
  });

  it("dedupes concurrent in-flight meta requests into a single transport call", async () => {
    let resolveFetch: ((value: ChapterListItem[]) => void) | undefined;
    const fetchAllChapterMeta = vi.fn(
      () =>
        new Promise<ChapterListItem[]>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    const store = createChapterStore(buildTransport({ fetchAllChapterMeta }));

    // Two callers before resolution share the same in-flight promise: transport runs once.
    const first = store.loadProjectChapterMeta("project-1");
    const second = store.loadProjectChapterMeta("project-1");

    expect(fetchAllChapterMeta).toHaveBeenCalledTimes(1);

    resolveFetch?.([makeListItem()]);
    const [firstResult, secondResult] = await Promise.all([first, second]);

    expect(firstResult).toEqual([makeListItem()]);
    expect(secondResult).toEqual(firstResult);
    expect(store.getMetaSnapshot("project-1").data).toHaveLength(1);
  });

  it("captures a meta transport error in the snapshot and re-throws it", async () => {
    const apiError = new ApiError({
      code: "NOT_FOUND",
      message: "project missing",
      requestId: "rid-meta",
      status: 404,
    });
    const store = createChapterStore(
      buildTransport({ fetchAllChapterMeta: vi.fn(async () => Promise.reject(apiError)) }),
    );

    await expect(store.loadProjectChapterMeta("project-1")).rejects.toBe(apiError);

    const snapshot = store.getMetaSnapshot("project-1");
    expect(snapshot.error).toBe(apiError);
    expect(snapshot.data).toBeNull();
    expect(snapshot.loading).toBe(false);
    expect(snapshot.hasLoaded).toBe(true);
    expect(snapshot.stale).toBe(true);
  });

  it("prefetchChapterDetail populates the detail cache on success", async () => {
    const store = createChapterStore(
      buildTransport({ fetchChapterDetail: async () => makeDetail({ title: "Prefetched" }) }),
    );

    await store.prefetchChapterDetail("chapter-1");

    const snapshot = store.getDetailSnapshot("chapter-1");
    expect(snapshot.data?.title).toBe("Prefetched");
    expect(snapshot.loading).toBe(false);
    expect(snapshot.error).toBeNull();
  });

  it("prefetchChapterDetail swallows load failures while still recording the error snapshot", async () => {
    const apiError = new ApiError({
      code: "NOT_FOUND",
      message: "chapter missing",
      requestId: "rid-detail",
      status: 404,
    });
    const store = createChapterStore(
      buildTransport({ fetchChapterDetail: vi.fn(async () => Promise.reject(apiError)) }),
    );

    // prefetch must not propagate the rejection.
    await expect(store.prefetchChapterDetail("chapter-1")).resolves.toBeUndefined();

    const snapshot = store.getDetailSnapshot("chapter-1");
    expect(snapshot.error).toBe(apiError);
    expect(snapshot.data).toBeNull();
    expect(snapshot.hasLoaded).toBe(true);
    expect(snapshot.stale).toBe(true);
  });

  it("records the project mapping from a detail load so getKnownProjectId resolves it", async () => {
    const store = createChapterStore(
      buildTransport({
        fetchChapterDetail: async () => makeDetail({ id: "chapter-9", project_id: "project-2" }),
      }),
    );

    // Before any detail is seen, the index has no mapping.
    expect(store.getKnownProjectId("chapter-9")).toBeNull();

    await store.loadChapterDetail("chapter-9");
    expect(store.getKnownProjectId("chapter-9")).toBe("project-2");
  });
});
