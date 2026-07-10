// @vitest-environment jsdom
// M45 回归：ImportPage 必须在 running 文档的 updated_at 停止变化后继续推进墙钟，
// 并在超过 5 分钟时自动显示停滞告警与重试入口。
import { describe, expect, it, vi, afterEach, beforeEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

// vi.mock 工厂会被提升到文件顶部执行，故用 vi.hoisted 声明桩，避免 TDZ（参考 ThemeToggle.test.tsx）。
const mocks = vi.hoisted(() => ({
  apiJson: vi.fn(),
  // toast 必须返回稳定引用：loadList/selectDocAndLoad 的 useCallback deps 含 toast，
  // 若每次 render 返回新对象 → 回调身份变化 → 挂载 effect/轮询 effect 每 render 重跑 → loadList 无限循环。
  toast: {
    toastSuccess: vi.fn(),
    toastWarning: vi.fn(),
    toastError: vi.fn(),
  } as const,
}));

vi.mock("@/services/apiClient", () => ({
  // ImportPage 仅用到 ApiError / apiJson / sanitizeFilename；提供三者即可。
  ApiError: class ApiError extends Error {
    code: string;
    requestId: string;
    status: number;
    constructor(args: { code: string; message: string; requestId: string; status: number }) {
      super(args.message);
      this.name = "ApiError";
      this.code = args.code;
      this.requestId = args.requestId;
      this.status = args.status;
    }
  },
  apiJson: mocks.apiJson,
  sanitizeFilename: (value: string) => value.trim(),
}));

// useToast 需 ToastProvider；用 no-op mock 隔离，避免拉入 portal/全局副作用。
// 返回 hoisted 的稳定引用（见上）。
vi.mock("@/components/ui/toast", () => ({
  useToast: () => mocks.toast,
}));

import { ImportPage } from "@/pages/ImportPage";

// M45：停滞复现基准时间。updated_at 冻结于此 → lastUpdateMs 恒定。
const BASE_MS = Date.parse("2026-01-15T10:00:00.000Z");
const FROZEN_UPDATED_AT = "2026-01-15T10:00:00.000Z";

// 冻结的 running 文档：updated_at = 基准时间。后续轮询始终返回同一冻结值（模拟后端停滞）。
const frozenDoc = {
  id: "doc-stall",
  project_id: "p1",
  actor_user_id: null,
  filename: "stall-test.txt",
  content_type: "txt",
  status: "running",
  progress: 10,
  progress_message: "处理中",
  chunk_count: 0,
  kb_id: null,
  error_message: null,
  created_at: FROZEN_UPDATED_AT,
  updated_at: FROZEN_UPDATED_AT,
};

const detailPayload = {
  document: frozenDoc,
  content_preview: "",
  vector_ingest_result: null,
  story_memory_proposal: null,
};

describe("ImportPage 停滞检测", () => {
  beforeEach(() => {
    // 用 fake timer 锁定基准时间，使 Date.now() 可控。
    vi.useFakeTimers({ now: BASE_MS });
    mocks.apiJson.mockImplementation((path: string) => {
      // 列表 GET /imports（frozen updated_at；后续轮询同样返回冻结值）
      if (path.endsWith("/imports")) {
        return Promise.resolve({ ok: true, data: { documents: [frozenDoc] }, request_id: "rid-list" });
      }
      // chunks 端点（本测试不触发，提供桩以防意外调用）
      if (path.includes("/chunks")) {
        return Promise.resolve({ ok: true, data: { chunks: [] }, request_id: "rid-chunks" });
      }
      // 详情 GET /imports/{id}
      return Promise.resolve({ ok: true, data: detailPayload, request_id: "rid-detail" });
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("status=running 且 updated_at 停滞超 5 分钟后应显示告警与重试按钮", async () => {
    // selectedId 机制：ImportPage 通过 ?docId= 查询参数在挂载 effect 内自动 selectDocAndLoad
    // （见 ImportPage.tsx:319-329）。projectId 来自 useParams（非 prop）。
    render(
      <MemoryRouter initialEntries={["/projects/p1/import?docId=doc-stall"]}>
        <Routes>
          <Route path="projects/:projectId/import" element={<ImportPage />} />
        </Routes>
      </MemoryRouter>,
    );

    // 刷新微任务：触发挂载 effect（loadList → selectDocAndLoad → setSelectedId → shouldPoll=true →
    // 轮询 interval 注册）。推进 1ms 仅刷微任务，不触发 2000ms 轮询。
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });

    // 前置确认：页面已加载并注册轮询。记录初始按钮数；停滞后唯一新增的结构化
    // action 应是 retry 按钮，不依赖其用户可见文案。
    expect(mocks.apiJson).toHaveBeenCalled();
    const initialButtonCount = screen.getAllByRole("button").length;

    // 推进 6 分钟+，跨过 5 分钟阈值；期间后端始终返回冻结的 updated_at。
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6 * 60_000 + 2000);
    });

    // 停滞后 action 区新增 retry 按钮，并显示明确告警。
    expect(screen.getAllByRole("button")).toHaveLength(initialButtonCount + 1);
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
    expect(screen.getByText(/超过 5 分钟未更新进度/)).toBeInTheDocument();
  });
});
