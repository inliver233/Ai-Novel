import { describe, expect, it, vi } from "vitest";

import { ApiError } from "@/services/apiClient";
import { createProjectTaskStore, type ProjectTaskListQuery } from "@/services/projectTaskStore";
import type { ProjectTaskRuntime } from "@/services/projectTaskRuntime";
import type { ProjectTask } from "@/types";

function makeTask(overrides: Partial<ProjectTask> = {}): ProjectTask {
  return {
    id: "task-1",
    project_id: "project-1",
    kind: "noop",
    status: "queued",
    actor_user_id: null,
    idempotency_key: "e2e:task-1",
    error_type: null,
    error_message: null,
    timings: { created_at: "2026-03-14T15:00:00Z" },
    params: null,
    result: null,
    error: null,
    ...overrides,
  };
}

function makeRuntime(overrides: Partial<ProjectTaskRuntime> = {}): ProjectTaskRuntime {
  return {
    run: makeTask(),
    timeline: [],
    checkpoints: [],
    steps: [],
    artifacts: [],
    batch: null,
    ...overrides,
  };
}

function buildTransport(
  args: {
    fetchProjectTaskDetail?: (taskId: string) => Promise<ProjectTask>;
    fetchProjectTaskRuntime?: (taskId: string) => Promise<ProjectTaskRuntime>;
    listProjectTasks?: (projectId: string, query: Required<ProjectTaskListQuery>) => Promise<ProjectTask[]>;
  } = {},
) {
  return {
    fetchProjectTaskDetail: args.fetchProjectTaskDetail ?? vi.fn(async () => makeTask()),
    fetchProjectTaskRuntime: args.fetchProjectTaskRuntime ?? vi.fn(async () => makeRuntime()),
    listProjectTasks: args.listProjectTasks ?? vi.fn(async () => []),
  };
}

describe("projectTaskStore (deeper interactions)", () => {
  it("fans out detail state changes to every active subscriber", async () => {
    const store = createProjectTaskStore(
      buildTransport({ fetchProjectTaskDetail: async () => makeTask({ id: "task-1", status: "running" }) }),
    );
    const listenerA = vi.fn();
    const listenerB = vi.fn();
    const unsubscribeA = store.subscribeProjectTaskDetail("task-1", listenerA);
    store.subscribeProjectTaskDetail("task-1", listenerB);

    await store.loadProjectTaskDetail("task-1");

    expect(listenerA).toHaveBeenCalled();
    expect(listenerB).toHaveBeenCalled();
    const callsA = listenerA.mock.calls.length;
    const callsB = listenerB.mock.calls.length;
    expect(callsA).toBeGreaterThan(0);
    expect(callsB).toBe(callsA);

    // Detaching one subscriber must silence only that one; the other keeps receiving.
    unsubscribeA();
    store.invalidateProjectTaskDetail("task-1");

    expect(listenerA.mock.calls.length).toBe(callsA);
    expect(listenerB.mock.calls.length).toBeGreaterThan(callsB);
  });

  it("fans out list state changes to every active subscriber", async () => {
    const store = createProjectTaskStore(
      buildTransport({ listProjectTasks: async () => [makeTask({ id: "task-a" })] }),
    );
    const listenerA = vi.fn();
    const listenerB = vi.fn();
    const unsubscribeA = store.subscribeProjectTaskList("project-1", {}, listenerA);
    store.subscribeProjectTaskList("project-1", {}, listenerB);

    await store.loadProjectTaskList("project-1");

    expect(listenerA).toHaveBeenCalled();
    expect(listenerB).toHaveBeenCalled();
    const callsA = listenerA.mock.calls.length;
    const callsB = listenerB.mock.calls.length;
    expect(callsA).toBe(callsB);

    unsubscribeA();
    store.invalidateProjectTaskLists("project-1");

    expect(listenerA.mock.calls.length).toBe(callsA);
    expect(listenerB.mock.calls.length).toBeGreaterThan(callsB);
  });

  it("fans out runtime state changes to every active subscriber", async () => {
    const store = createProjectTaskStore(
      buildTransport({ fetchProjectTaskRuntime: async () => makeRuntime() }),
    );
    const listenerA = vi.fn();
    const listenerB = vi.fn();
    const unsubscribeA = store.subscribeProjectTaskRuntime("task-1", listenerA);
    store.subscribeProjectTaskRuntime("task-1", listenerB);

    await store.loadProjectTaskRuntime("task-1");

    expect(listenerA).toHaveBeenCalled();
    expect(listenerB).toHaveBeenCalled();
    const callsA = listenerA.mock.calls.length;
    const callsB = listenerB.mock.calls.length;
    expect(callsA).toBe(callsB);

    unsubscribeA();
    store.invalidateProjectTaskRuntime("task-1");

    expect(listenerA.mock.calls.length).toBe(callsA);
    expect(listenerB.mock.calls.length).toBeGreaterThan(callsB);
  });

  it("refetches detail after invalidateProjectTaskDetail marks it stale", async () => {
    const fetchProjectTaskDetail = vi.fn(async () => makeTask());
    const store = createProjectTaskStore(buildTransport({ fetchProjectTaskDetail }));

    await store.loadProjectTaskDetail("task-1");
    expect(fetchProjectTaskDetail).toHaveBeenCalledTimes(1);
    expect(store.getProjectTaskDetailSnapshot("task-1").stale).toBe(false);

    store.invalidateProjectTaskDetail("task-1");
    expect(store.getProjectTaskDetailSnapshot("task-1").stale).toBe(true);

    // Stale bypasses the cache check even without { force: true }, so the transport fires again.
    await store.loadProjectTaskDetail("task-1");
    expect(fetchProjectTaskDetail).toHaveBeenCalledTimes(2);
    expect(store.getProjectTaskDetailSnapshot("task-1").stale).toBe(false);
  });

  it("dedupes concurrent in-flight detail requests into a single transport call", async () => {
    let resolveFetch: ((value: ProjectTask) => void) | undefined;
    const fetchProjectTaskDetail = vi.fn(
      () =>
        new Promise<ProjectTask>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    const store = createProjectTaskStore(buildTransport({ fetchProjectTaskDetail }));

    // Two callers before resolution share the same in-flight promise: transport runs once.
    const first = store.loadProjectTaskDetail("task-1");
    const second = store.loadProjectTaskDetail("task-1");

    expect(fetchProjectTaskDetail).toHaveBeenCalledTimes(1);
    // While the request is in flight, the snapshot reflects loading.
    expect(store.getProjectTaskDetailSnapshot("task-1").loading).toBe(true);

    const resolved = makeTask({ id: "task-1", status: "running" });
    resolveFetch?.(resolved);
    const [firstResult, secondResult] = await Promise.all([first, second]);

    expect(firstResult).toEqual(resolved);
    expect(secondResult).toEqual(firstResult);
    expect(store.getProjectTaskDetailSnapshot("task-1").data).toEqual(resolved);
    expect(store.getProjectTaskDetailSnapshot("task-1").loading).toBe(false);
  });

  it("dedupes concurrent in-flight list requests into a single transport call", async () => {
    let resolveFetch: ((value: ProjectTask[]) => void) | undefined;
    const listProjectTasks = vi.fn(
      () =>
        new Promise<ProjectTask[]>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    const store = createProjectTaskStore(buildTransport({ listProjectTasks }));

    const first = store.loadProjectTaskList("project-1");
    const second = store.loadProjectTaskList("project-1");

    expect(listProjectTasks).toHaveBeenCalledTimes(1);
    expect(store.getProjectTaskListSnapshot("project-1").loading).toBe(true);

    const tasks = [makeTask({ id: "task-a" }), makeTask({ id: "task-b" })];
    resolveFetch?.(tasks);
    const [firstResult, secondResult] = await Promise.all([first, second]);

    expect(firstResult).toEqual(tasks);
    expect(secondResult).toEqual(firstResult);
    expect(store.getProjectTaskListSnapshot("project-1").data).toHaveLength(2);
    expect(store.getProjectTaskListSnapshot("project-1").loading).toBe(false);
  });

  it("captures a detail transport error in the snapshot and re-throws it", async () => {
    const apiError = new ApiError({
      code: "NOT_FOUND",
      message: "task missing",
      requestId: "rid-detail",
      status: 404,
    });
    const store = createProjectTaskStore(
      buildTransport({ fetchProjectTaskDetail: vi.fn(async () => Promise.reject(apiError)) }),
    );

    await expect(store.loadProjectTaskDetail("task-1")).rejects.toBe(apiError);

    const snapshot = store.getProjectTaskDetailSnapshot("task-1");
    expect(snapshot.error).toBe(apiError);
    expect(snapshot.data).toBeNull();
    expect(snapshot.loading).toBe(false);
    expect(snapshot.hasLoaded).toBe(true);
    expect(snapshot.stale).toBe(true);
  });

  it("normalizes a non-ApiError failure into an UNKNOWN ApiError snapshot", async () => {
    const store = createProjectTaskStore(
      buildTransport({ fetchProjectTaskDetail: vi.fn(async () => Promise.reject(new Error("boom"))) }),
    );

    await expect(store.loadProjectTaskDetail("task-1")).rejects.toBeInstanceOf(ApiError);

    const snapshot = store.getProjectTaskDetailSnapshot("task-1");
    expect(snapshot.error).toBeInstanceOf(ApiError);
    expect(snapshot.error?.code).toBe("UNKNOWN");
    expect(snapshot.error?.message).toBe("boom");
    expect(snapshot.data).toBeNull();
    expect(snapshot.hasLoaded).toBe(true);
    expect(snapshot.loading).toBe(false);
    expect(snapshot.stale).toBe(true);
  });

  it("records the project mapping from detail and runtime loads so tryGetProjectIdForTask resolves it", async () => {
    const store = createProjectTaskStore(
      buildTransport({
        fetchProjectTaskDetail: async () => makeTask({ id: "task-d", project_id: "project-detail" }),
        fetchProjectTaskRuntime: async () =>
          makeRuntime({ run: makeTask({ id: "task-r", project_id: "project-runtime" }) }),
      }),
    );

    // Before any load, the index has no mapping for either task.
    expect(store.tryGetProjectIdForTask("task-d")).toBeNull();
    expect(store.tryGetProjectIdForTask("task-r")).toBeNull();

    await store.loadProjectTaskDetail("task-d");
    expect(store.tryGetProjectIdForTask("task-d")).toBe("project-detail");

    await store.loadProjectTaskRuntime("task-r");
    expect(store.tryGetProjectIdForTask("task-r")).toBe("project-runtime");
  });

  it("touchProjectTaskDetail seeds the project index and notifies detail subscribers", async () => {
    const store = createProjectTaskStore(buildTransport());
    const listener = vi.fn();
    store.subscribeProjectTaskDetail("task-touch", listener);

    expect(store.tryGetProjectIdForTask("task-touch")).toBeNull();
    expect(store.getProjectTaskDetailSnapshot("task-touch").data).toBeNull();

    store.touchProjectTaskDetail("task-touch", "project-touch");

    expect(listener).toHaveBeenCalledTimes(1);
    expect(store.tryGetProjectIdForTask("task-touch")).toBe("project-touch");
    // The entry now exists but no data has been loaded into it.
    expect(store.getProjectTaskDetailSnapshot("task-touch").data).toBeNull();
  });
});
