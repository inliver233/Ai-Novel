import { ApiError, apiJson } from "./apiClient";
import { toApiError } from "./apiError";
import { getProjectTaskRuntime, type ProjectTaskRuntime } from "./projectTaskRuntime";
import type { ProjectTask } from "../types";

export type ProjectTaskResourceSnapshot<T> = Readonly<{
  data: T | null;
  error: ApiError | null;
  hasLoaded: boolean;
  loading: boolean;
  stale: boolean;
}>;

export type ProjectTaskListSnapshot = ProjectTaskResourceSnapshot<ProjectTask[]>;
export type ProjectTaskDetailSnapshot = ProjectTaskResourceSnapshot<ProjectTask>;
export type ProjectTaskRuntimeSnapshot = ProjectTaskResourceSnapshot<ProjectTaskRuntime>;

export type ProjectTaskListQuery = {
  status?: string | null;
  limit?: number;
};

export type ProjectTaskTransport = {
  fetchProjectTaskDetail: (taskId: string) => Promise<ProjectTask>;
  fetchProjectTaskRuntime: (taskId: string) => Promise<ProjectTaskRuntime>;
  listProjectTasks: (projectId: string, query: Required<ProjectTaskListQuery>) => Promise<ProjectTask[]>;
};

type CacheEntry<T> = {
  promise: Promise<T> | null;
  snapshot: ProjectTaskResourceSnapshot<T>;
};

const EMPTY_LIST_SNAPSHOT: ProjectTaskListSnapshot = Object.freeze({
  data: null,
  error: null,
  hasLoaded: false,
  loading: false,
  stale: false,
});

const EMPTY_DETAIL_SNAPSHOT: ProjectTaskDetailSnapshot = Object.freeze({
  data: null,
  error: null,
  hasLoaded: false,
  loading: false,
  stale: false,
});

const EMPTY_RUNTIME_SNAPSHOT: ProjectTaskRuntimeSnapshot = Object.freeze({
  data: null,
  error: null,
  hasLoaded: false,
  loading: false,
  stale: false,
});

const DEFAULT_TRANSPORT: ProjectTaskTransport = {
  fetchProjectTaskDetail: async (taskId) => {
    const res = await apiJson<ProjectTask>(`/api/tasks/${encodeURIComponent(taskId)}`);
    return res.data;
  },
  fetchProjectTaskRuntime: getProjectTaskRuntime,
  listProjectTasks: async (projectId, query) => {
    const params = new URLSearchParams();
    if (query.status) params.set("status", query.status);
    params.set("limit", String(query.limit));
    const qs = params.toString();
    const res = await apiJson<{ items: ProjectTask[] }>(
      `/api/projects/${encodeURIComponent(projectId)}/tasks${qs ? `?${qs}` : ""}`,
    );
    return res.data.items ?? [];
  },
};

function normalizeListQuery(query: ProjectTaskListQuery = {}): Required<ProjectTaskListQuery> {
  const normalizedStatus =
    typeof query.status === "string" && query.status.trim() && query.status.trim() !== "all" ? query.status.trim() : "";
  return {
    status: normalizedStatus,
    limit: typeof query.limit === "number" && Number.isFinite(query.limit) ? query.limit : 50,
  };
}

function nextSnapshot<T>(
  entry: CacheEntry<T>,
  patch: Partial<ProjectTaskResourceSnapshot<T>>,
): ProjectTaskResourceSnapshot<T> {
  entry.snapshot = { ...entry.snapshot, ...patch };
  return entry.snapshot;
}

function buildListKey(projectId: string, query: Required<ProjectTaskListQuery>): string {
  return `${projectId}::${query.status || "all"}::${query.limit}`;
}

export function createProjectTaskStore(transport: ProjectTaskTransport = DEFAULT_TRANSPORT) {
  const listEntries = new Map<string, CacheEntry<ProjectTask[]>>();
  const detailEntries = new Map<string, CacheEntry<ProjectTask>>();
  const runtimeEntries = new Map<string, CacheEntry<ProjectTaskRuntime>>();
  const listListeners = new Map<string, Set<() => void>>();
  const detailListeners = new Map<string, Set<() => void>>();
  const runtimeListeners = new Map<string, Set<() => void>>();
  const projectListIndex = new Map<string, Set<string>>();
  const taskProjectIndex = new Map<string, string>();

  const ensureListEntry = (projectId: string, query: Required<ProjectTaskListQuery>): CacheEntry<ProjectTask[]> => {
    const key = buildListKey(projectId, query);
    const existing = listEntries.get(key);
    if (existing) return existing;
    const created: CacheEntry<ProjectTask[]> = { promise: null, snapshot: EMPTY_LIST_SNAPSHOT };
    listEntries.set(key, created);
    const keys = projectListIndex.get(projectId) ?? new Set<string>();
    keys.add(key);
    projectListIndex.set(projectId, keys);
    return created;
  };

  const ensureDetailEntry = (taskId: string): CacheEntry<ProjectTask> => {
    const existing = detailEntries.get(taskId);
    if (existing) return existing;
    const created: CacheEntry<ProjectTask> = { promise: null, snapshot: EMPTY_DETAIL_SNAPSHOT };
    detailEntries.set(taskId, created);
    return created;
  };

  const ensureRuntimeEntry = (taskId: string): CacheEntry<ProjectTaskRuntime> => {
    const existing = runtimeEntries.get(taskId);
    if (existing) return existing;
    const created: CacheEntry<ProjectTaskRuntime> = { promise: null, snapshot: EMPTY_RUNTIME_SNAPSHOT };
    runtimeEntries.set(taskId, created);
    return created;
  };

  const emitList = (projectId: string, query: Required<ProjectTaskListQuery>) => {
    const key = buildListKey(projectId, query);
    for (const listener of listListeners.get(key) ?? []) listener();
  };

  const emitDetail = (taskId: string) => {
    for (const listener of detailListeners.get(taskId) ?? []) listener();
  };

  const emitRuntime = (taskId: string) => {
    for (const listener of runtimeListeners.get(taskId) ?? []) listener();
  };

  const setProjectTaskDetail = (task: ProjectTask) => {
    const entry = ensureDetailEntry(task.id);
    taskProjectIndex.set(task.id, task.project_id);
    nextSnapshot(entry, {
      data: task,
      error: null,
      hasLoaded: true,
      loading: false,
      stale: false,
    });
    emitDetail(task.id);
  };

  return {
    getProjectTaskDetailSnapshot: (taskId: string): ProjectTaskDetailSnapshot =>
      detailEntries.get(taskId)?.snapshot ?? EMPTY_DETAIL_SNAPSHOT,
    getProjectTaskListSnapshot: (projectId: string, query: ProjectTaskListQuery = {}): ProjectTaskListSnapshot =>
      listEntries.get(buildListKey(projectId, normalizeListQuery(query)))?.snapshot ?? EMPTY_LIST_SNAPSHOT,
    getProjectTaskRuntimeSnapshot: (taskId: string): ProjectTaskRuntimeSnapshot =>
      runtimeEntries.get(taskId)?.snapshot ?? EMPTY_RUNTIME_SNAPSHOT,
    invalidateProjectTaskDetail: (taskId: string) => {
      const entry = detailEntries.get(taskId);
      if (!entry) return;
      nextSnapshot(entry, { stale: true });
      emitDetail(taskId);
    },
    invalidateProjectTaskLists: (projectId: string) => {
      for (const key of projectListIndex.get(projectId) ?? []) {
        const entry = listEntries.get(key);
        if (!entry) continue;
        nextSnapshot(entry, { stale: true });
        for (const listener of listListeners.get(key) ?? []) listener();
      }
    },
    invalidateProjectTaskRuntime: (taskId: string) => {
      const entry = runtimeEntries.get(taskId);
      if (!entry) return;
      nextSnapshot(entry, { stale: true });
      emitRuntime(taskId);
    },
    loadProjectTaskDetail: async (taskId: string, options: { force?: boolean } = {}): Promise<ProjectTask> => {
      const entry = ensureDetailEntry(taskId);
      if (!options.force && entry.snapshot.data && !entry.snapshot.stale) return entry.snapshot.data;
      if (entry.promise) return entry.promise;

      nextSnapshot(entry, { error: null, loading: true });
      emitDetail(taskId);

      entry.promise = transport
        .fetchProjectTaskDetail(taskId)
        .then((task) => {
          setProjectTaskDetail(task);
          return detailEntries.get(taskId)?.snapshot.data as ProjectTask;
        })
        .catch((error) => {
          nextSnapshot(entry, {
            error: toApiError(error),
            hasLoaded: true,
            loading: false,
            stale: true,
          });
          emitDetail(taskId);
          throw entry.snapshot.error;
        })
        .finally(() => {
          entry.promise = null;
        });

      return entry.promise;
    },
    loadProjectTaskList: async (
      projectId: string,
      query: ProjectTaskListQuery = {},
      options: { force?: boolean } = {},
    ): Promise<ProjectTask[]> => {
      const normalized = normalizeListQuery(query);
      const entry = ensureListEntry(projectId, normalized);
      if (!options.force && entry.snapshot.data && !entry.snapshot.stale) return entry.snapshot.data;
      if (entry.promise) return entry.promise;

      nextSnapshot(entry, { error: null, loading: true });
      emitList(projectId, normalized);

      entry.promise = transport
        .listProjectTasks(projectId, normalized)
        .then((tasks) => {
          for (const task of tasks) {
            taskProjectIndex.set(task.id, task.project_id);
          }
          nextSnapshot(entry, {
            data: tasks,
            error: null,
            hasLoaded: true,
            loading: false,
            stale: false,
          });
          emitList(projectId, normalized);
          return listEntries.get(buildListKey(projectId, normalized))?.snapshot.data ?? [];
        })
        .catch((error) => {
          nextSnapshot(entry, {
            error: toApiError(error),
            hasLoaded: true,
            loading: false,
            stale: true,
          });
          emitList(projectId, normalized);
          throw entry.snapshot.error;
        })
        .finally(() => {
          entry.promise = null;
        });

      return entry.promise;
    },
    loadProjectTaskRuntime: async (taskId: string, options: { force?: boolean } = {}): Promise<ProjectTaskRuntime> => {
      const entry = ensureRuntimeEntry(taskId);
      if (!options.force && entry.snapshot.data && !entry.snapshot.stale) return entry.snapshot.data;
      if (entry.promise) return entry.promise;

      nextSnapshot(entry, { error: null, loading: true });
      emitRuntime(taskId);

      entry.promise = transport
        .fetchProjectTaskRuntime(taskId)
        .then((runtime) => {
          const projectId = runtime.run?.project_id;
          if (typeof projectId === "string" && projectId.trim()) {
            taskProjectIndex.set(taskId, projectId);
          }
          nextSnapshot(entry, {
            data: runtime,
            error: null,
            hasLoaded: true,
            loading: false,
            stale: false,
          });
          emitRuntime(taskId);
          return runtimeEntries.get(taskId)?.snapshot.data as ProjectTaskRuntime;
        })
        .catch((error) => {
          nextSnapshot(entry, {
            error: toApiError(error),
            hasLoaded: true,
            loading: false,
            stale: true,
          });
          emitRuntime(taskId);
          throw entry.snapshot.error;
        })
        .finally(() => {
          entry.promise = null;
        });

      return entry.promise;
    },
    setProjectTaskDetail,
    subscribeProjectTaskDetail: (taskId: string, listener: () => void) => {
      const listeners = detailListeners.get(taskId) ?? new Set<() => void>();
      listeners.add(listener);
      detailListeners.set(taskId, listeners);
      return () => {
        const next = detailListeners.get(taskId);
        next?.delete(listener);
        if (!next || next.size === 0) detailListeners.delete(taskId);
      };
    },
    subscribeProjectTaskList: (projectId: string, query: ProjectTaskListQuery = {}, listener: () => void) => {
      const normalized = normalizeListQuery(query);
      const key = buildListKey(projectId, normalized);
      const listeners = listListeners.get(key) ?? new Set<() => void>();
      listeners.add(listener);
      listListeners.set(key, listeners);
      const keys = projectListIndex.get(projectId) ?? new Set<string>();
      keys.add(key);
      projectListIndex.set(projectId, keys);
      return () => {
        const next = listListeners.get(key);
        next?.delete(listener);
        if (!next || next.size === 0) listListeners.delete(key);
      };
    },
    subscribeProjectTaskRuntime: (taskId: string, listener: () => void) => {
      const listeners = runtimeListeners.get(taskId) ?? new Set<() => void>();
      listeners.add(listener);
      runtimeListeners.set(taskId, listeners);
      return () => {
        const next = runtimeListeners.get(taskId);
        next?.delete(listener);
        if (!next || next.size === 0) runtimeListeners.delete(taskId);
      };
    },
    touchProjectTaskDetail: (taskId: string, projectId?: string | null) => {
      if (typeof projectId === "string" && projectId.trim()) {
        taskProjectIndex.set(taskId, projectId.trim());
      }
      ensureDetailEntry(taskId);
      emitDetail(taskId);
    },
    tryGetProjectIdForTask: (taskId: string): string | null => taskProjectIndex.get(taskId) ?? null,
  };
}

export const projectTaskStore = createProjectTaskStore();
export const EMPTY_PROJECT_TASK_LIST_SNAPSHOT = EMPTY_LIST_SNAPSHOT;
export const EMPTY_PROJECT_TASK_DETAIL_SNAPSHOT = EMPTY_DETAIL_SNAPSHOT;
export const EMPTY_PROJECT_TASK_RUNTIME_SNAPSHOT = EMPTY_RUNTIME_SNAPSHOT;
