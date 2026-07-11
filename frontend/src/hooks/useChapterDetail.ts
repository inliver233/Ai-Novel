import { useCallback, useEffect, useSyncExternalStore } from "react";

import { chapterStore } from "../services/chapterStore";
import type { ChapterDetail } from "../types";
import type { ApiError } from "../services/apiClient";

const EMPTY_SNAPSHOT = Object.freeze({
  data: null,
  error: null,
  hasLoaded: false,
  loading: false,
  stale: false,
});

export function useChapterDetail(
  chapterId: string | null | undefined,
  options: { enabled?: boolean } = {},
): {
  chapter: ChapterDetail | null;
  error: ApiError | null;
  hasData: boolean;
  hasLoaded: boolean;
  loading: boolean;
  refresh: () => Promise<ChapterDetail | null>;
  stale: boolean;
} {
  const enabled = options.enabled ?? true;

  const subscribe = useCallback(
    (onStoreChange: () => void) => {
      if (!chapterId) return () => undefined;
      return chapterStore.subscribeDetail(chapterId, onStoreChange);
    },
    [chapterId],
  );

  const getSnapshot = useCallback(() => {
    if (!chapterId) return EMPTY_SNAPSHOT;
    return chapterStore.getDetailSnapshot(chapterId);
  }, [chapterId]);

  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  useEffect(() => {
    if (!chapterId || !enabled) return;
    void chapterStore.loadChapterDetail(chapterId).catch(() => undefined);
  }, [chapterId, enabled]);

  const refresh = useCallback(async () => {
    if (!chapterId || !enabled) return null;
    return chapterStore.loadChapterDetail(chapterId, { force: true });
  }, [chapterId, enabled]);

  return {
    chapter: snapshot.data,
    error: snapshot.error,
    hasData: snapshot.data !== null,
    hasLoaded: snapshot.hasLoaded,
    loading: snapshot.loading,
    refresh,
    stale: snapshot.stale,
  };
}
