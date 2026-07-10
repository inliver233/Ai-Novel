import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";

import { useToast } from "../components/ui/toast";
import { toastApiError } from "../lib/apiErrorPresentation";
import { createRequestSeqGuard } from "../lib/requestSeqGuard";
import { ApiError } from "../services/apiClient";

export type ProjectDataResult<T> = {
  data: T | null;
  setData: Dispatch<SetStateAction<T | null>>;
  error: ApiError | null;
  loading: boolean;
  refresh: () => Promise<void>;
  resetError: () => void;
};

export type UseProjectDataOptions = {
  toastOnError?: boolean;
};

function normalizeProjectDataError(error: unknown): ApiError {
  if (error instanceof ApiError) return error;
  return new ApiError({
    code: "UNKNOWN_ERROR",
    message: "请求失败",
    requestId: "unknown",
    status: 0,
    details: error,
  });
}

export function useProjectData<T>(
  projectId: string | undefined,
  loader: (projectId: string) => Promise<T>,
  options: UseProjectDataOptions = {},
): ProjectDataResult<T> {
  const toast = useToast();
  const loaderRef = useRef(loader);
  const toastOnErrorRef = useRef(options.toastOnError ?? false);
  const requestGuardRef = useRef(createRequestSeqGuard());
  const activeProjectIdRef = useRef<string | undefined>(undefined);

  useEffect(() => {
    loaderRef.current = loader;
  }, [loader]);

  useEffect(() => {
    toastOnErrorRef.current = options.toastOnError ?? false;
  }, [options.toastOnError]);

  useEffect(() => {
    const guard = requestGuardRef.current;
    return () => {
      guard.invalidate();
    };
  }, []);

  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState<boolean>(Boolean(projectId));
  const resetError = useCallback(() => setError(null), []);

  const refresh = useCallback(async () => {
    if (!projectId) return;
    const seq = requestGuardRef.current.next();
    setError(null);
    setLoading(true);
    try {
      const next = await loaderRef.current(projectId);
      if (!requestGuardRef.current.isLatest(seq)) return;
      setData(next);
    } catch (e) {
      if (!requestGuardRef.current.isLatest(seq)) return;
      const nextError = normalizeProjectDataError(e);
      setError(nextError);
      if (toastOnErrorRef.current) toastApiError(toast, nextError);
    } finally {
      if (requestGuardRef.current.isLatest(seq)) {
        setLoading(false);
      }
    }
  }, [projectId, toast]);

  useEffect(() => {
    const projectChanged = activeProjectIdRef.current !== projectId;
    activeProjectIdRef.current = projectId;
    if (!projectId) {
      requestGuardRef.current.invalidate();
      setData(null);
      setError(null);
      setLoading(false);
      return;
    }
    if (projectChanged) {
      requestGuardRef.current.invalidate();
      setData(null);
      setError(null);
      setLoading(true);
    }
    void refresh();
  }, [projectId, refresh]);

  return { data, setData, error, loading, refresh, resetError };
}
