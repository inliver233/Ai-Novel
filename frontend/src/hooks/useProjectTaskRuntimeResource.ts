import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";

import { useToast } from "../components/ui/toast";
import { toastApiError } from "../lib/apiErrorPresentation";
import {
  EMPTY_PROJECT_TASK_DETAIL_SNAPSHOT,
  EMPTY_PROJECT_TASK_LIST_SNAPSHOT,
  EMPTY_PROJECT_TASK_RUNTIME_SNAPSHOT,
  projectTaskStore,
  type ProjectTaskListSnapshot,
  type ProjectTaskDetailSnapshot,
  type ProjectTaskRuntimeSnapshot,
} from "../services/projectTaskStore";
import { ApiError } from "../services/apiClient";

import {
  useProjectTaskEvents,
  type ProjectTaskEventEnvelope,
  type ProjectTaskEventsSnapshot,
} from "./useProjectTaskEvents";

type RefreshOptions = {
  force?: boolean;
  silent?: boolean;
};

type DetailRefreshOptions = RefreshOptions & {
  taskId?: string | null;
};

type ListRefreshOptions = RefreshOptions & {
  projectId?: string;
  status?: string | null;
  limit?: number;
};

type RuntimeRefreshOptions = RefreshOptions & {
  taskId?: string | null;
};

function normalizeApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error;
  return new ApiError({
    code: "UNKNOWN",
    message: error instanceof Error ? error.message : String(error),
    requestId: "unknown",
    status: 0,
    details: error,
  });
}

function normalizeTaskId(taskId: string | null | undefined): string | null {
  const next = String(taskId || "").trim();
  return next || null;
}

function normalizeListStatus(status: string | null | undefined): string | null {
  const next = String(status || "").trim();
  if (!next || next === "all") return null;
  return next;
}

export function useProjectTaskListResource(args: {
  projectId?: string;
  status?: string | null;
  limit?: number;
  enabled?: boolean;
}): ProjectTaskListSnapshot & { refresh: (options?: ListRefreshOptions) => Promise<void> } {
  const toast = useToast();
  const enabled = args.enabled ?? true;
  const status = normalizeListStatus(args.status);
  const limit = typeof args.limit === "number" && Number.isFinite(args.limit) ? args.limit : 50;
  const projectId = args.projectId;

  const snapshot = useSyncExternalStore(
    useCallback(
      (listener) => {
        if (!projectId || !enabled) return () => undefined;
        return projectTaskStore.subscribeProjectTaskList(projectId, { status, limit }, listener);
      },
      [enabled, limit, projectId, status],
    ),
    useCallback(() => {
      if (!projectId || !enabled) return EMPTY_PROJECT_TASK_LIST_SNAPSHOT;
      return projectTaskStore.getProjectTaskListSnapshot(projectId, { status, limit });
    }, [enabled, limit, projectId, status]),
    () => EMPTY_PROJECT_TASK_LIST_SNAPSHOT,
  );

  const refresh = useCallback(
    async (options: ListRefreshOptions = {}) => {
      const targetProjectId = options.projectId ?? projectId;
      if (!targetProjectId || !enabled) return;
      const targetStatus = normalizeListStatus(options.status ?? status);
      const targetLimit = typeof options.limit === "number" && Number.isFinite(options.limit) ? options.limit : limit;
      try {
        await projectTaskStore.loadProjectTaskList(
          targetProjectId,
          { status: targetStatus, limit: targetLimit },
          { force: options.force },
        );
      } catch (error) {
        if (options.silent) return;
        const err = normalizeApiError(error);
        toastApiError(toast, err);
      }
    },
    [enabled, limit, projectId, status, toast],
  );

  useEffect(() => {
    if (!projectId || !enabled) return;
    if (!snapshot.hasLoaded || snapshot.stale) {
      void refresh({ silent: true });
    }
  }, [enabled, projectId, refresh, snapshot.hasLoaded, snapshot.stale]);

  return { ...snapshot, refresh };
}

export function useProjectTaskDetailResource(args: {
  taskId?: string | null;
  enabled?: boolean;
}): ProjectTaskDetailSnapshot & { refresh: (options?: DetailRefreshOptions) => Promise<void> } {
  const toast = useToast();
  const enabled = args.enabled ?? true;
  const taskId = normalizeTaskId(args.taskId);

  const snapshot = useSyncExternalStore(
    useCallback(
      (listener) => {
        if (!taskId || !enabled) return () => undefined;
        return projectTaskStore.subscribeProjectTaskDetail(taskId, listener);
      },
      [enabled, taskId],
    ),
    useCallback(() => {
      if (!taskId || !enabled) return EMPTY_PROJECT_TASK_DETAIL_SNAPSHOT;
      return projectTaskStore.getProjectTaskDetailSnapshot(taskId);
    }, [enabled, taskId]),
    () => EMPTY_PROJECT_TASK_DETAIL_SNAPSHOT,
  );

  const refresh = useCallback(
    async (options: DetailRefreshOptions = {}) => {
      const targetTaskId = normalizeTaskId(options.taskId ?? taskId);
      if (!targetTaskId || !enabled) return;
      try {
        await projectTaskStore.loadProjectTaskDetail(targetTaskId, { force: options.force });
      } catch (error) {
        if (options.silent) return;
        const err = normalizeApiError(error);
        toastApiError(toast, err);
      }
    },
    [enabled, taskId, toast],
  );

  useEffect(() => {
    if (!taskId || !enabled) return;
    if (!snapshot.hasLoaded || snapshot.stale) {
      void refresh({ silent: true });
    }
  }, [enabled, refresh, snapshot.hasLoaded, snapshot.stale, taskId]);

  return { ...snapshot, refresh };
}

export function useProjectTaskRuntimeResource(args: {
  taskId?: string | null;
  enabled?: boolean;
}): ProjectTaskRuntimeSnapshot & { refresh: (options?: RuntimeRefreshOptions) => Promise<void> } {
  const toast = useToast();
  const enabled = args.enabled ?? true;
  const taskId = normalizeTaskId(args.taskId);

  const snapshot = useSyncExternalStore(
    useCallback(
      (listener) => {
        if (!taskId || !enabled) return () => undefined;
        return projectTaskStore.subscribeProjectTaskRuntime(taskId, listener);
      },
      [enabled, taskId],
    ),
    useCallback(() => {
      if (!taskId || !enabled) return EMPTY_PROJECT_TASK_RUNTIME_SNAPSHOT;
      return projectTaskStore.getProjectTaskRuntimeSnapshot(taskId);
    }, [enabled, taskId]),
    () => EMPTY_PROJECT_TASK_RUNTIME_SNAPSHOT,
  );

  const refresh = useCallback(
    async (options: RuntimeRefreshOptions = {}) => {
      const targetTaskId = normalizeTaskId(options.taskId ?? taskId);
      if (!targetTaskId || !enabled) return;
      try {
        await projectTaskStore.loadProjectTaskRuntime(targetTaskId, { force: options.force });
      } catch (error) {
        if (options.silent) return;
        const err = normalizeApiError(error);
        toastApiError(toast, err);
      }
    },
    [enabled, taskId, toast],
  );

  useEffect(() => {
    if (!taskId || !enabled) return;
    if (!snapshot.hasLoaded || snapshot.stale) {
      void refresh({ silent: true });
    }
  }, [enabled, refresh, snapshot.hasLoaded, snapshot.stale, taskId]);

  return { ...snapshot, refresh };
}

export function useProjectTaskLiveSync(args: {
  projectId?: string;
  enabled?: boolean;
  trackedTaskId?: string | null;
  pollWhen?: boolean;
  pollIntervalMs?: number;
  debounceMs?: number;
  refreshOnIdleSnapshot?: boolean;
  pickSnapshotTaskId?: (snapshot: ProjectTaskEventsSnapshot) => string | null | undefined;
  shouldRefreshOnEvent?: (event: ProjectTaskEventEnvelope, trackedTaskId: string | null) => boolean;
  onRefresh: (taskId?: string | null) => void;
}) {
  const {
    projectId,
    enabled = true,
    trackedTaskId,
    pollWhen = false,
    pollIntervalMs = 8000,
    debounceMs = 120,
    refreshOnIdleSnapshot = false,
    pickSnapshotTaskId,
    shouldRefreshOnEvent,
    onRefresh,
  } = args;

  const refreshTimerRef = useRef<number | null>(null);
  const trackedTaskIdRef = useRef<string | null>(normalizeTaskId(trackedTaskId));
  const pickSnapshotTaskIdRef = useRef(pickSnapshotTaskId);
  const shouldRefreshOnEventRef = useRef(shouldRefreshOnEvent);
  const onRefreshRef = useRef(onRefresh);

  useEffect(() => {
    trackedTaskIdRef.current = normalizeTaskId(trackedTaskId);
  }, [trackedTaskId]);

  useEffect(() => {
    pickSnapshotTaskIdRef.current = pickSnapshotTaskId;
  }, [pickSnapshotTaskId]);

  useEffect(() => {
    shouldRefreshOnEventRef.current = shouldRefreshOnEvent;
  }, [shouldRefreshOnEvent]);

  useEffect(() => {
    onRefreshRef.current = onRefresh;
  }, [onRefresh]);

  useEffect(() => {
    return () => {
      if (refreshTimerRef.current !== null) {
        window.clearTimeout(refreshTimerRef.current);
      }
    };
  }, []);

  const scheduleRefresh = useCallback(
    (taskId?: string | null) => {
      if (refreshTimerRef.current !== null) {
        window.clearTimeout(refreshTimerRef.current);
      }
      refreshTimerRef.current = window.setTimeout(() => {
        refreshTimerRef.current = null;
        onRefreshRef.current(taskId ?? trackedTaskIdRef.current);
      }, debounceMs);
    },
    [debounceMs],
  );

  const events = useProjectTaskEvents({
    projectId,
    enabled,
    onSnapshot: (snapshot) => {
      const snapshotTaskId = normalizeTaskId(pickSnapshotTaskIdRef.current?.(snapshot));
      if (snapshotTaskId) {
        scheduleRefresh(snapshotTaskId);
        return;
      }
      if (refreshOnIdleSnapshot && trackedTaskIdRef.current) {
        scheduleRefresh(trackedTaskIdRef.current);
      }
    },
    onEvent: (event) => {
      const currentTaskId = trackedTaskIdRef.current;
      if (shouldRefreshOnEventRef.current && !shouldRefreshOnEventRef.current(event, currentTaskId)) {
        return;
      }
      scheduleRefresh(event.task_id);
    },
  });

  useEffect(() => {
    if (!projectId || !enabled || !pollWhen) return;
    if (events.status === "open") return;
    const intervalId = window.setInterval(() => {
      onRefreshRef.current(trackedTaskIdRef.current);
    }, pollIntervalMs);
    return () => window.clearInterval(intervalId);
  }, [enabled, events.status, pollIntervalMs, pollWhen, projectId]);

  return { status: events.status };
}
