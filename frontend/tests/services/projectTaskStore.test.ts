import { describe, expect, it, vi } from "vitest";

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

describe("projectTaskStore", () => {
  it("caches project task lists per filter until invalidated", async () => {
    const listProjectTasks = vi.fn(async (projectId: string, query: Required<ProjectTaskListQuery>) => [
      makeTask({ id: `${projectId}-${query.status || "all"}` }),
    ]);
    const store = createProjectTaskStore({
      listProjectTasks: (projectId, query) => listProjectTasks(projectId, query),
      fetchProjectTaskDetail: vi.fn(async () => makeTask()),
      fetchProjectTaskRuntime: vi.fn(async () => makeRuntime()),
    });

    await store.loadProjectTaskList("project-1", { status: "queued", limit: 50 });
    await store.loadProjectTaskList("project-1", { status: "queued", limit: 50 });
    expect(listProjectTasks).toHaveBeenCalledTimes(1);

    await store.loadProjectTaskList("project-1", { status: "failed", limit: 50 });
    expect(listProjectTasks).toHaveBeenCalledTimes(2);

    store.invalidateProjectTaskLists("project-1");
    await store.loadProjectTaskList("project-1", { status: "queued", limit: 50 });
    await store.loadProjectTaskList("project-1", { status: "failed", limit: 50 });
    expect(listProjectTasks).toHaveBeenCalledTimes(4);
  });

  it("treats all-status list queries as the unfiltered cache key", async () => {
    const listProjectTasks = vi.fn(async (projectId: string, query: Required<ProjectTaskListQuery>) => [
      makeTask({ id: `${projectId}-all-${query.limit}` }),
    ]);
    const store = createProjectTaskStore({
      listProjectTasks: (projectId, query) => listProjectTasks(projectId, query),
      fetchProjectTaskDetail: vi.fn(async () => makeTask()),
      fetchProjectTaskRuntime: vi.fn(async () => makeRuntime()),
    });

    await store.loadProjectTaskList("project-1", { status: "all", limit: 50 });
    await store.loadProjectTaskList("project-1", { status: "", limit: 50 });

    expect(listProjectTasks).toHaveBeenCalledTimes(1);
    expect(listProjectTasks).toHaveBeenCalledWith("project-1", { status: "", limit: 50 });
  });

  it("keeps detail snapshots in sync when detail is written back from a mutation", async () => {
    const fetchProjectTaskDetail = vi.fn(async () => makeTask());
    const store = createProjectTaskStore({
      listProjectTasks: vi.fn(async () => []),
      fetchProjectTaskDetail,
      fetchProjectTaskRuntime: vi.fn(async () => makeRuntime()),
    });

    await store.loadProjectTaskDetail("task-1");
    store.setProjectTaskDetail(makeTask({ id: "task-1", status: "failed", error_message: "boom" }));

    expect(store.getProjectTaskDetailSnapshot("task-1").data).toEqual(
      expect.objectContaining({ id: "task-1", status: "failed", error_message: "boom" }),
    );
    expect(fetchProjectTaskDetail).toHaveBeenCalledTimes(1);
  });

  it("caches runtime until invalidated or forced", async () => {
    const fetchProjectTaskRuntime = vi.fn(async () => makeRuntime());
    const store = createProjectTaskStore({
      listProjectTasks: vi.fn(async () => []),
      fetchProjectTaskDetail: vi.fn(async () => makeTask()),
      fetchProjectTaskRuntime,
    });

    await store.loadProjectTaskRuntime("task-1");
    await store.loadProjectTaskRuntime("task-1");
    expect(fetchProjectTaskRuntime).toHaveBeenCalledTimes(1);

    store.invalidateProjectTaskRuntime("task-1");
    await store.loadProjectTaskRuntime("task-1");
    expect(fetchProjectTaskRuntime).toHaveBeenCalledTimes(2);

    await store.loadProjectTaskRuntime("task-1", { force: true });
    expect(fetchProjectTaskRuntime).toHaveBeenCalledTimes(3);
  });
});
