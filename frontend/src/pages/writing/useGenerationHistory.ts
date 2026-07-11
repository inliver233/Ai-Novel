import { toastApiError } from "../../lib/apiErrorPresentation";
import { toApiError } from "../../services/apiError";
import { useCallback, useEffect, useRef, useState } from "react";

import type { GenerationRun } from "../../components/writing/types";
import { createRequestSeqGuard } from "../../lib/requestSeqGuard";
import { apiJson } from "../../services/apiClient";

export function useGenerationHistory(args: {
  projectId: string | undefined;
  toast: { toastError: (message: string, requestId?: string) => void };
}) {
  const { projectId, toast } = args;

  const [open, setOpen] = useState(false);
  const [runsLoading, setRunsLoading] = useState(false);
  const [runs, setRuns] = useState<GenerationRun[]>([]);
  const [selectedRun, setSelectedRun] = useState<GenerationRun | null>(null);
  const runsGuardRef = useRef(createRequestSeqGuard());
  const runDetailsGuardRef = useRef(createRequestSeqGuard());

  useEffect(() => {
    const runsGuard = runsGuardRef.current;
    const runDetailsGuard = runDetailsGuardRef.current;
    return () => {
      runsGuard.invalidate();
      runDetailsGuard.invalidate();
    };
  }, []);

  const refreshRuns = useCallback(async () => {
    if (!projectId) return;
    const seq = runsGuardRef.current.next();
    setRunsLoading(true);
    try {
      const res = await apiJson<{ runs: GenerationRun[] }>(`/api/projects/${projectId}/generation_runs?limit=5`);
      if (!runsGuardRef.current.isLatest(seq)) return;
      setRuns(res.data.runs);
      setSelectedRun((prev) => {
        if (prev && res.data.runs.some((r) => r.id === prev.id)) return prev;
        return res.data.runs[0] ?? null;
      });
    } catch (e) {
      if (!runsGuardRef.current.isLatest(seq)) return;
      const err = toApiError(e);
      toastApiError(toast, err);
    } finally {
      if (runsGuardRef.current.isLatest(seq)) {
        setRunsLoading(false);
      }
    }
  }, [projectId, toast]);

  const openDrawer = useCallback(() => {
    setOpen(true);
    void refreshRuns();
  }, [refreshRuns]);

  const closeDrawer = useCallback(() => setOpen(false), []);

  const selectRun = useCallback(
    async (run: GenerationRun) => {
      const seq = runDetailsGuardRef.current.next();
      setSelectedRun(run);
      try {
        const res = await apiJson<{ run: GenerationRun }>(`/api/generation_runs/${run.id}`);
        if (!runDetailsGuardRef.current.isLatest(seq)) return;
        setSelectedRun(res.data.run);
      } catch (e) {
        if (!runDetailsGuardRef.current.isLatest(seq)) return;
        const err = toApiError(e);
        toastApiError(toast, err);
      }
    },
    [toast],
  );

  return { open, openDrawer, closeDrawer, runsLoading, runs, selectedRun, refreshRuns, selectRun };
}
