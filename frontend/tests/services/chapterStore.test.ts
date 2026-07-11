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

describe("chapterStore", () => {
  it("stores unknown transport failures as safe ApiError instances", async () => {
    const thrown = { reason: "offline" };
    const store = createChapterStore(
      buildTransport({ fetchAllChapterMeta: vi.fn(async () => Promise.reject(thrown)) }),
    );

    await expect(store.loadProjectChapterMeta("project-1")).rejects.toMatchObject({
      code: "UNKNOWN",
      details: thrown,
    });
    expect(store.getMetaSnapshot("project-1").error).toBeInstanceOf(ApiError);
    expect(store.getMetaSnapshot("project-1").error).toMatchObject({
      code: "UNKNOWN",
      message: "请求失败",
      requestId: "unknown",
      status: 0,
      details: thrown,
    });
  });

  it("caches chapter meta until forced or invalidated", async () => {
    const fetchAllChapterMeta = vi.fn(async () => [makeListItem()]);
    const store = createChapterStore(buildTransport({ fetchAllChapterMeta }));

    await store.loadProjectChapterMeta("project-1");
    await store.loadProjectChapterMeta("project-1");
    expect(fetchAllChapterMeta).toHaveBeenCalledTimes(1);

    store.invalidateProjectChapters("project-1");
    await store.loadProjectChapterMeta("project-1");
    expect(fetchAllChapterMeta).toHaveBeenCalledTimes(2);

    await store.loadProjectChapterMeta("project-1", { force: true });
    expect(fetchAllChapterMeta).toHaveBeenCalledTimes(3);
  });

  it("keeps chapter detail and meta flags in sync after updates", async () => {
    const updateChapter = vi.fn(async () =>
      makeDetail({
        content_md: "# Ready",
        summary: "Synced",
        status: "done",
        title: "Chapter 1 Updated",
      }),
    );
    const store = createChapterStore(
      buildTransport({
        fetchAllChapterMeta: async () => [makeListItem()],
        updateChapter,
      }),
    );

    await store.loadProjectChapterMeta("project-1");
    const chapter = await store.updateChapterDetail("chapter-1", { title: "Chapter 1 Updated", status: "done" });

    expect(chapter.title).toBe("Chapter 1 Updated");
    expect(store.getDetailSnapshot("chapter-1").data?.summary).toBe("Synced");
    expect(store.getMetaSnapshot("project-1").data).toEqual([
      expect.objectContaining({ title: "Chapter 1 Updated", has_content: true, has_summary: true, status: "done" }),
    ]);
    expect(updateChapter).toHaveBeenCalledTimes(1);
  });

  it("removes deleted chapters from both meta and detail caches", async () => {
    const store = createChapterStore(
      buildTransport({
        fetchAllChapterMeta: async () => [makeListItem()],
        fetchChapterDetail: async () => makeDetail(),
      }),
    );

    await store.loadProjectChapterMeta("project-1");
    await store.loadChapterDetail("chapter-1");
    await store.deleteProjectChapter("chapter-1", { projectId: "project-1" });

    expect(store.getMetaSnapshot("project-1").data).toEqual([]);
    expect(store.getDetailSnapshot("chapter-1").data).toBeNull();
  });

  it("replaces project meta and drops removed detail cache entries after bulk create", async () => {
    const store = createChapterStore(
      buildTransport({
        bulkCreateChapters: async () => [
          makeDetail({ id: "chapter-2", number: 2, title: "Chapter 2" }),
          makeDetail({ id: "chapter-3", number: 3, title: "Chapter 3" }),
        ],
        fetchAllChapterMeta: async () => [makeListItem()],
        fetchChapterDetail: async () => makeDetail(),
      }),
    );

    await store.loadProjectChapterMeta("project-1");
    await store.loadChapterDetail("chapter-1");
    await store.bulkCreateProjectChapters(
      "project-1",
      {
        chapters: [
          { number: 2, title: "Chapter 2", plan: null },
          { number: 3, title: "Chapter 3", plan: null },
        ],
      },
      { replace: true },
    );

    expect(store.getMetaSnapshot("project-1").data?.map((chapter) => chapter.id)).toEqual(["chapter-2", "chapter-3"]);
    expect(store.getDetailSnapshot("chapter-1").data).toBeNull();
  });
});
