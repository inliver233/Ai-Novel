import { useCallback, useEffect, useSyncExternalStore } from "react";

import type { ApiError } from "../services/apiClient";
import { chapterStore } from "../services/chapterStore";
import type { ChapterListItem } from "../types";

const EMPTY_CHAPTERS = [] as const;
const EMPTY_SNAPSHOT = Object.freeze({
  data: null,
  error: null,
  hasLoaded: false,
  loading: false,
  stale: false,
});

export function useChapterMetaList(projectId: string | undefined): {
  chapters: readonly ChapterListItem[];
  error: ApiError | null;
  hasData: boolean;
  hasLoaded: boolean;
  loading: boolean;
  refresh: () => Promise<ChapterListItem[]>;
  stale: boolean;
} {
  const subscribe = useCallback(
    (onStoreChange: () => void) => {
      if (!projectId) return () => undefined;
      return chapterStore.subscribeMeta(projectId, onStoreChange);
    },
    [projectId],
  );

  const getSnapshot = useCallback(() => {
    if (!projectId) return EMPTY_SNAPSHOT;
    return chapterStore.getMetaSnapshot(projectId);
  }, [projectId]);

  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  useEffect(() => {
    if (!projectId) return;
    void chapterStore.loadProjectChapterMeta(projectId).catch(() => undefined);
  }, [projectId]);

  // Auto-refetch when cache is invalidated (e.g., from outline page creating chapters)
  useEffect(() => {
    if (!projectId || !snapshot.stale) return;
    void chapterStore.loadProjectChapterMeta(projectId).catch(() => undefined);
  }, [projectId, snapshot.stale]);

  const refresh = useCallback(async () => {
    if (!projectId) return [...EMPTY_CHAPTERS];
    return chapterStore.loadProjectChapterMeta(projectId, { force: true });
  }, [projectId]);

  return {
    chapters: snapshot.data ?? EMPTY_CHAPTERS,
    error: snapshot.error,
    hasData: snapshot.data !== null,
    hasLoaded: snapshot.hasLoaded,
    loading: snapshot.loading,
    refresh,
    stale: snapshot.stale,
  };
}
