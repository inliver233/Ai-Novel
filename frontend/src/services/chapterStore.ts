import { ApiError } from "./apiClient";
import { toApiError } from "./apiError";
import {
  bulkCreateChapters,
  chapterDetailToListItem,
  createChapter,
  deleteChapter,
  fetchAllChapterMeta,
  fetchChapterDetail,
  updateChapter,
} from "./chaptersApi";
import type {
  BulkCreateChapterInput,
  ChapterDetail,
  ChapterListItem,
  CreateChapterInput,
  UpdateChapterInput,
} from "../types";

export type ChapterResourceSnapshot<T> = Readonly<{
  data: T | null;
  error: ApiError | null;
  hasLoaded: boolean;
  loading: boolean;
  stale: boolean;
}>;

export type ChapterMetaSnapshot = ChapterResourceSnapshot<ChapterListItem[]>;
export type ChapterDetailSnapshot = ChapterResourceSnapshot<ChapterDetail>;

export type ChapterTransport = {
  bulkCreateChapters: (
    projectId: string,
    payload: BulkCreateChapterInput,
    options?: { replace?: boolean },
  ) => Promise<ChapterDetail[]>;
  createChapter: (projectId: string, payload: CreateChapterInput) => Promise<ChapterDetail>;
  deleteChapter: (chapterId: string) => Promise<void>;
  fetchAllChapterMeta: (projectId: string) => Promise<ChapterListItem[]>;
  fetchChapterDetail: (chapterId: string) => Promise<ChapterDetail>;
  updateChapter: (chapterId: string, payload: UpdateChapterInput) => Promise<ChapterDetail>;
};

type CacheEntry<T> = {
  promise: Promise<T> | null;
  snapshot: ChapterResourceSnapshot<T>;
};

const EMPTY_META_SNAPSHOT: ChapterMetaSnapshot = Object.freeze({
  data: null,
  error: null,
  hasLoaded: false,
  loading: false,
  stale: false,
});

const EMPTY_DETAIL_SNAPSHOT: ChapterDetailSnapshot = Object.freeze({
  data: null,
  error: null,
  hasLoaded: false,
  loading: false,
  stale: false,
});

const DEFAULT_TRANSPORT: ChapterTransport = {
  bulkCreateChapters,
  createChapter,
  deleteChapter,
  fetchAllChapterMeta,
  fetchChapterDetail,
  updateChapter,
};

function sortChapterMeta(chapters: ChapterListItem[]): ChapterListItem[] {
  return [...chapters].sort((left, right) => (left.number ?? 0) - (right.number ?? 0));
}

function nextSnapshot<T>(entry: CacheEntry<T>, patch: Partial<ChapterResourceSnapshot<T>>): ChapterResourceSnapshot<T> {
  entry.snapshot = { ...entry.snapshot, ...patch };
  return entry.snapshot;
}

export function createChapterStore(transport: ChapterTransport = DEFAULT_TRANSPORT) {
  const metaEntries = new Map<string, CacheEntry<ChapterListItem[]>>();
  const detailEntries = new Map<string, CacheEntry<ChapterDetail>>();
  const metaListeners = new Map<string, Set<() => void>>();
  const detailListeners = new Map<string, Set<() => void>>();
  const chapterProjectIndex = new Map<string, string>();
  const projectChapterIndex = new Map<string, Set<string>>();

  const ensureMetaEntry = (projectId: string): CacheEntry<ChapterListItem[]> => {
    const existing = metaEntries.get(projectId);
    if (existing) return existing;
    const created: CacheEntry<ChapterListItem[]> = { promise: null, snapshot: EMPTY_META_SNAPSHOT };
    metaEntries.set(projectId, created);
    return created;
  };

  const ensureDetailEntry = (chapterId: string): CacheEntry<ChapterDetail> => {
    const existing = detailEntries.get(chapterId);
    if (existing) return existing;
    const created: CacheEntry<ChapterDetail> = { promise: null, snapshot: EMPTY_DETAIL_SNAPSHOT };
    detailEntries.set(chapterId, created);
    return created;
  };

  const emitMeta = (projectId: string) => {
    for (const listener of metaListeners.get(projectId) ?? []) listener();
  };

  const emitDetail = (chapterId: string) => {
    for (const listener of detailListeners.get(chapterId) ?? []) listener();
  };

  const indexProjectChapters = (projectId: string, chapterIds: Iterable<string>) => {
    projectChapterIndex.set(projectId, new Set(chapterIds));
    for (const chapterId of chapterIds) {
      chapterProjectIndex.set(chapterId, projectId);
    }
  };

  const dropProjectDetails = (projectId: string, keepIds?: Set<string>) => {
    for (const chapterId of projectChapterIndex.get(projectId) ?? []) {
      if (keepIds?.has(chapterId)) continue;
      detailEntries.delete(chapterId);
      emitDetail(chapterId);
      if (chapterProjectIndex.get(chapterId) === projectId) {
        chapterProjectIndex.delete(chapterId);
      }
    }
  };

  const replaceProjectMeta = (projectId: string, chapters: ChapterListItem[]) => {
    const entry = ensureMetaEntry(projectId);
    const sorted = sortChapterMeta(chapters);
    const nextIds = new Set(sorted.map((chapter) => chapter.id));
    dropProjectDetails(projectId, nextIds);
    indexProjectChapters(projectId, nextIds);
    nextSnapshot(entry, {
      data: sorted,
      error: null,
      hasLoaded: true,
      loading: false,
      stale: false,
    });
    emitMeta(projectId);
  };

  const upsertMetaItem = (projectId: string, chapter: ChapterListItem) => {
    const entry = ensureMetaEntry(projectId);
    const current = entry.snapshot.data;
    const nextIds = new Set(projectChapterIndex.get(projectId) ?? []);
    nextIds.add(chapter.id);
    indexProjectChapters(projectId, nextIds);

    if (!current) {
      if (entry.snapshot.hasLoaded) {
        nextSnapshot(entry, { stale: true });
        emitMeta(projectId);
      }
      return;
    }

    const existingIndex = current.findIndex((item) => item.id === chapter.id);
    const next =
      existingIndex >= 0 ? current.map((item) => (item.id === chapter.id ? chapter : item)) : [...current, chapter];
    nextSnapshot(entry, { data: sortChapterMeta(next), error: null, hasLoaded: true, loading: false, stale: false });
    emitMeta(projectId);
  };

  const removeMetaItem = (projectId: string | undefined, chapterId: string) => {
    if (!projectId) return;
    const entry = ensureMetaEntry(projectId);
    const nextIds = new Set(projectChapterIndex.get(projectId) ?? []);
    nextIds.delete(chapterId);
    indexProjectChapters(projectId, nextIds);
    chapterProjectIndex.delete(chapterId);

    if (!entry.snapshot.data) {
      if (entry.snapshot.hasLoaded) {
        nextSnapshot(entry, { stale: true });
        emitMeta(projectId);
      }
      return;
    }

    nextSnapshot(entry, {
      data: entry.snapshot.data.filter((chapter) => chapter.id !== chapterId),
      error: null,
      hasLoaded: true,
      loading: false,
      stale: false,
    });
    emitMeta(projectId);
  };

  const setDetail = (chapter: ChapterDetail) => {
    const entry = ensureDetailEntry(chapter.id);
    chapterProjectIndex.set(chapter.id, chapter.project_id);
    const nextIds = new Set(projectChapterIndex.get(chapter.project_id) ?? []);
    nextIds.add(chapter.id);
    indexProjectChapters(chapter.project_id, nextIds);
    nextSnapshot(entry, {
      data: chapter,
      error: null,
      hasLoaded: true,
      loading: false,
      stale: false,
    });
    emitDetail(chapter.id);
  };

  const loadMeta = async (projectId: string, options: { force?: boolean } = {}): Promise<ChapterListItem[]> => {
    const entry = ensureMetaEntry(projectId);
    if (!options.force && entry.snapshot.data && !entry.snapshot.stale) return entry.snapshot.data;
    if (entry.promise) return entry.promise;

    nextSnapshot(entry, { error: null, loading: true });
    emitMeta(projectId);

    entry.promise = transport
      .fetchAllChapterMeta(projectId)
      .then((chapters) => {
        replaceProjectMeta(projectId, chapters);
        return metaEntries.get(projectId)?.snapshot.data ?? [];
      })
      .catch((error) => {
        nextSnapshot(entry, {
          error: toApiError(error),
          hasLoaded: true,
          loading: false,
          stale: true,
        });
        emitMeta(projectId);
        throw entry.snapshot.error;
      })
      .finally(() => {
        entry.promise = null;
      });

    return entry.promise;
  };

  const loadDetail = async (chapterId: string, options: { force?: boolean } = {}): Promise<ChapterDetail> => {
    const entry = ensureDetailEntry(chapterId);
    if (!options.force && entry.snapshot.data && !entry.snapshot.stale) return entry.snapshot.data;
    if (entry.promise) return entry.promise;

    nextSnapshot(entry, { error: null, loading: true });
    emitDetail(chapterId);

    entry.promise = transport
      .fetchChapterDetail(chapterId)
      .then((chapter) => {
        setDetail(chapter);
        upsertMetaItem(chapter.project_id, chapterDetailToListItem(chapter));
        return detailEntries.get(chapterId)?.snapshot.data as ChapterDetail;
      })
      .catch((error) => {
        nextSnapshot(entry, {
          error: toApiError(error),
          hasLoaded: true,
          loading: false,
          stale: true,
        });
        emitDetail(chapterId);
        throw entry.snapshot.error;
      })
      .finally(() => {
        entry.promise = null;
      });

    return entry.promise;
  };

  return {
    bulkCreateProjectChapters: async (
      projectId: string,
      payload: BulkCreateChapterInput,
      options: { replace?: boolean } = {},
    ): Promise<ChapterDetail[]> => {
      const chapters = await transport.bulkCreateChapters(projectId, payload, options);
      replaceProjectMeta(
        projectId,
        chapters.map((chapter) => chapterDetailToListItem(chapter)),
      );
      if (options.replace) {
        const keepIds = new Set(chapters.map((chapter) => chapter.id));
        dropProjectDetails(projectId, keepIds);
      }
      return chapters;
    },
    createProjectChapter: async (projectId: string, payload: CreateChapterInput): Promise<ChapterDetail> => {
      const chapter = await transport.createChapter(projectId, payload);
      setDetail(chapter);
      upsertMetaItem(projectId, chapterDetailToListItem(chapter));
      return chapter;
    },
    deleteProjectChapter: async (chapterId: string, options: { projectId?: string } = {}): Promise<void> => {
      const projectId =
        options.projectId ??
        chapterProjectIndex.get(chapterId) ??
        detailEntries.get(chapterId)?.snapshot.data?.project_id;
      await transport.deleteChapter(chapterId);
      detailEntries.delete(chapterId);
      emitDetail(chapterId);
      removeMetaItem(projectId, chapterId);
    },
    getDetailSnapshot: (chapterId: string): ChapterDetailSnapshot =>
      detailEntries.get(chapterId)?.snapshot ?? EMPTY_DETAIL_SNAPSHOT,
    getKnownProjectId: (chapterId: string): string | null => chapterProjectIndex.get(chapterId) ?? null,
    getMetaSnapshot: (projectId: string): ChapterMetaSnapshot =>
      metaEntries.get(projectId)?.snapshot ?? EMPTY_META_SNAPSHOT,
    invalidateChapterDetail: (chapterId: string) => {
      const entry = detailEntries.get(chapterId);
      if (!entry) return;
      nextSnapshot(entry, { stale: true });
      emitDetail(chapterId);
    },
    invalidateProjectChapters: (projectId: string, options: { dropDetails?: boolean } = {}) => {
      const entry = ensureMetaEntry(projectId);
      nextSnapshot(entry, { stale: true });
      emitMeta(projectId);
      if (options.dropDetails) {
        dropProjectDetails(projectId);
      }
    },
    loadChapterDetail: loadDetail,
    loadProjectChapterMeta: loadMeta,
    prefetchChapterDetail: async (chapterId: string): Promise<void> => {
      try {
        await loadDetail(chapterId);
      } catch {
        return;
      }
    },
    subscribeDetail: (chapterId: string, listener: () => void) => {
      const listeners = detailListeners.get(chapterId) ?? new Set<() => void>();
      listeners.add(listener);
      detailListeners.set(chapterId, listeners);
      return () => {
        const next = detailListeners.get(chapterId);
        next?.delete(listener);
        if (!next || next.size === 0) detailListeners.delete(chapterId);
      };
    },
    subscribeMeta: (projectId: string, listener: () => void) => {
      const listeners = metaListeners.get(projectId) ?? new Set<() => void>();
      listeners.add(listener);
      metaListeners.set(projectId, listeners);
      return () => {
        const next = metaListeners.get(projectId);
        next?.delete(listener);
        if (!next || next.size === 0) metaListeners.delete(projectId);
      };
    },
    updateChapterDetail: async (chapterId: string, payload: UpdateChapterInput): Promise<ChapterDetail> => {
      const chapter = await transport.updateChapter(chapterId, payload);
      setDetail(chapter);
      upsertMetaItem(chapter.project_id, chapterDetailToListItem(chapter));
      return chapter;
    },
  };
}

export type ChapterStore = ReturnType<typeof createChapterStore>;

export const chapterStore = createChapterStore();
