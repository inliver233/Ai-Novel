import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";

import { useToast } from "../components/ui/toast";
import { toastApiError } from "../lib/apiErrorPresentation";
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
  hasLoaded: boolean;
  loading: boolean;
  refresh: () => Promise<ChapterDetail | null>;
  stale: boolean;
} {
  const toast = useToast();
  const lastErrorKeyRef = useRef<string | null>(null);
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
    void chapterStore.loadChapterDetail(chapterId);
  }, [chapterId, enabled]);

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
    if (!chapterId || !enabled) return null;
    return chapterStore.loadChapterDetail(chapterId, { force: true });
  }, [chapterId, enabled]);

  return {
    chapter: snapshot.data,
    error: snapshot.error,
    hasLoaded: snapshot.hasLoaded,
    loading: snapshot.loading,
    refresh,
    stale: snapshot.stale,
  };
}
