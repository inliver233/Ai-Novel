import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";

import { useToast } from "../components/ui/toast";
import { toastApiError } from "../lib/apiErrorPresentation";
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
  hasLoaded: boolean;
  loading: boolean;
  refresh: () => Promise<ChapterListItem[]>;
  stale: boolean;
} {
  const toast = useToast();
  const lastErrorKeyRef = useRef<string | null>(null);

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
    void chapterStore.loadProjectChapterMeta(projectId);
  }, [projectId]);

  // Auto-refetch when cache is invalidated (e.g., from outline page creating chapters)
  useEffect(() => {
    if (!projectId || !snapshot.stale) return;
    void chapterStore.loadProjectChapterMeta(projectId);
  }, [projectId, snapshot.stale]);

  useEffect(() => {
    if (!snapshot.error) {
      lastErrorKeyRef.current = null;
      return;
    }
    const errorKey = `${snapshot.error.code}:${snapshot.error.requestId}:${snapshot.error.message}`;
    if (lastErrorKeyRef.current === errorKey) return;
    lastErrorKeyRef.current = errorKey;
    toastApiError(toast, snapshot.error);
  }, [snapshot.error, toast]);

  const refresh = useCallback(async () => {
    if (!projectId) return [...EMPTY_CHAPTERS];
    return chapterStore.loadProjectChapterMeta(projectId, { force: true });
  }, [projectId]);

  return {
    chapters: snapshot.data ?? EMPTY_CHAPTERS,
    error: snapshot.error,
    hasLoaded: snapshot.hasLoaded,
    loading: snapshot.loading,
    refresh,
    stale: snapshot.stale,
  };
}
