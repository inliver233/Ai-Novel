// @vitest-environment jsdom
// catalog: frontend-pages#P4
// 回归：usePromptsPageState.saveAll 的 catch 块必须先规整化 unknown 错误，禁止直接
// 当作 ApiError 解构，导致非 ApiError 抛出（如编程错误或其它未规整化异常）时
// err.code / err.requestId 字段不存在 → toast 文案变成 "boom (undefined)"。规整化必须
// 为非 ApiError 提供稳定兜底，同时原样保留真实 ApiError 的 code/requestId。
//
// 修复前证据：performSaveAll catch 曾直接执行以下裸断言：
//   } catch (e) {
//     const err = e as ApiError;                                   // ← 裸断言，无 instanceof
//     toast.toastError(`${err.message} (${err.code})`, err.requestId);
//     return false;
//   }
//
// 正确行为：任何抛出值必须先规整化为 ApiError（或安全兜底）再取字段，使 toast 永不出现
// "(undefined)" 子串。非 ApiError 没有真实 requestId，规整化只使用 sentinel `unknown`，
// 不伪造可追溯的请求 ID。
//
// 隔离策略（参考 tests/hooks/useQueuedSave.test.tsx 的同款 page-state hook 挂载模式）：
// heavy 依赖（router/toast/confirm/wizard/autoSave/saveHotkey/persistentOutlet）全部 vi.mock
// 桩化，使 saveAll → apiJson 成为唯一网络出口；GET 走加载 fixture，PUT /llm_preset 抛 TypeError
// （非 ApiError）以精确命中目标 catch。toast 用模块级稳定引用，避免回调身份抖动引发渲染循环。
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

// vi.mock 工厂会被提升到文件顶部执行，故被引用的桩用 vi.hoisted 声明，避免 TDZ。
// toast / wizard 必须返回【引用稳定】的对象（其返回值被用作 useCallback 依赖），否则每次
// render 产生新引用 → saveAll 等回调身份变化 → effect 反复触发 → 无限渲染循环。
const mocks = vi.hoisted(() => ({
  apiJson: vi.fn(),
  navigate: vi.fn(),
  toast: {
    toastSuccess: vi.fn(),
    toastWarning: vi.fn(),
    toastError: vi.fn(),
  },
  confirm: {
    confirm: vi.fn(async () => true),
    choose: vi.fn(async () => "cancel" as const),
  },
  wizard: {
    loading: false,
    progress: { steps: [], nextStep: null },
    refresh: vi.fn(async () => {}),
    bumpLocal: vi.fn(),
  },
}));

// ApiError 用与生产同构的最小类（构造签名与 src/services/apiClient.ts 一致），
// 使源码内 `e instanceof ApiError` 判定与测试内 new ApiError(...) 指向同一（mock）类。
vi.mock("@/services/apiClient", () => ({
  ApiError: class ApiError extends Error {
    code: string;
    requestId: string;
    status: number;
    details?: unknown;
    constructor(args: { code: string; message: string; requestId: string; status: number; details?: unknown }) {
      super(args.message);
      this.name = "ApiError";
      this.code = args.code;
      this.requestId = args.requestId;
      this.status = args.status;
      this.details = args.details;
    }
  },
  apiJson: mocks.apiJson,
}));
vi.mock("@/components/ui/toast", () => ({ useToast: () => mocks.toast }));
vi.mock("@/components/ui/confirm", () => ({ useConfirm: () => mocks.confirm }));
vi.mock("react-router-dom", () => ({
  useParams: () => ({ projectId: "p1" }),
  useNavigate: () => mocks.navigate,
}));
vi.mock("@/hooks/usePersistentOutlet", () => ({ usePersistentOutletIsActive: () => false }));
vi.mock("@/hooks/useWizardProgress", () => ({
  useWizardProgress: () => mocks.wizard,
}));
vi.mock("@/hooks/useAutoSave", () => ({
  // 桩化为 no-op controller，避免自动保存定时器在手测保存路径上干扰。
  useAutoSave: () => ({ cancel: vi.fn(), flush: vi.fn() }),
}));
vi.mock("@/hooks/useSaveHotkey", () => ({ useSaveHotkey: () => {} }));

import { usePromptsPageState } from "@/pages/prompts/usePromptsPageState";
import type { LlmForm } from "@/components/prompts/types";
import { ApiError } from "@/services/apiClient";

// ---- 加载期 GET fixture（reloadAll 一次性并发 6 个 GET）----
const PRESET_FIXTURE = {
  project_id: "p1",
  provider: "openai",
  base_url: "https://api.openai.com/v1",
  model: "baseline-model",
  temperature: 0.7,
  top_p: 1,
  max_tokens: 1024,
  presence_penalty: 0,
  frequency_penalty: 0,
  top_k: 0,
  stop: [] as string[],
  timeout_seconds: 60,
  extra: {} as Record<string, unknown>,
};

// 控制位：是否让 PUT /llm_preset 抛出指定错误。默认加载期 GET 全成功。
let presetPutShouldFail = false;
let presetPutError: unknown = new TypeError("boom");

function installControlledApiJson() {
  mocks.apiJson.mockImplementation(async (url: string, init?: { method?: string }) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const u = String(url);

    // 保存失败路径：PUT /llm_preset 抛出当前用例指定的错误。
    if (method === "PUT" && u.includes("/llm_preset")) {
      if (presetPutShouldFail) throw presetPutError;
      return { data: { llm_preset: PRESET_FIXTURE }, request_id: "rid-save-ok" };
    }

    // 加载期 GET
    if (u.endsWith("/llm_preset")) return { data: { llm_preset: PRESET_FIXTURE }, request_id: "rid-preset" };
    if (u.match(/\/api\/projects\/p1$/)) {
      // llm_profile_id: null → saveAll 跳过 profile PUT，仅发 /llm_preset 一个 PUT。
      return { data: { project: { id: "p1", llm_profile_id: null } }, request_id: "rid-proj" };
    }
    if (u.endsWith("/api/llm_profiles")) return { data: { profiles: [] }, request_id: "rid-prof" };
    if (u.endsWith("/settings")) return { data: { settings: {} }, request_id: "rid-set" };
    if (u.endsWith("/llm_task_presets")) return { data: { catalog: [], task_presets: [] }, request_id: "rid-task" };
    if (u.endsWith("/api/vector_rag_profiles")) return { data: { profiles: [] }, request_id: "rid-vrag" };
    // capabilities 等其它 GET：返回最小值，失败也由源码 catch 吞掉。
    if (u.startsWith("/api/llm_capabilities")) return { data: { capabilities: {} }, request_id: "rid-cap" };
    return { data: {}, request_id: "rid-default" };
  });
}

beforeEach(() => {
  presetPutShouldFail = false;
  presetPutError = new TypeError("boom");
  mocks.apiJson.mockReset();
  installControlledApiJson();
  mocks.toast.toastSuccess.mockClear();
  mocks.toast.toastWarning.mockClear();
  mocks.toast.toastError.mockClear();
});

async function saveDirtyPresetWithError(error: unknown) {
  const { result } = renderHook(() => usePromptsPageState());

  await waitFor(() => {
    expect(result.current.loading).toBe(false);
  });

  act(() => {
    result.current.llmPresetPanelProps.setLlmForm((prev: LlmForm) => ({
      ...prev,
      model: "dirty-model",
    }));
  });
  await waitFor(() => {
    expect(result.current.llmPresetPanelProps.presetDirty).toBe(true);
  });

  presetPutShouldFail = true;
  presetPutError = error;

  let ok: boolean | undefined;
  await act(async () => {
    ok = await result.current.wizardBarProps.onSave!();
  });

  expect(ok).toBe(false);
  expect(mocks.toast.toastError).toHaveBeenCalled();
  expect(mocks.apiJson).toHaveBeenCalledWith("/api/projects/p1/llm_preset", expect.objectContaining({ method: "PUT" }));
  return mocks.toast.toastError.mock.calls.at(-1)!;
}

describe("usePromptsPageState.saveAll error normalization", () => {
  it("PUT 抛 TypeError 时显示稳定 UNKNOWN 错误", async () => {
    const lastCall = await saveDirtyPresetWithError(new TypeError("boom"));
    const message = lastCall[0] as string;

    expect(message).toBe("boom (UNKNOWN)");
    expect(lastCall[1]).toBe("unknown");
  });

  it("保留真实 ApiError 的 code 与 requestId", async () => {
    const lastCall = await saveDirtyPresetWithError(
      new ApiError({
        code: "CONFLICT",
        message: "保存冲突",
        requestId: "rid-conflict",
        status: 409,
      }),
    );

    expect(lastCall).toEqual(["保存冲突 (CONFLICT)", "rid-conflict"]);
  });

  it("空抛出值使用稳定兜底文案", async () => {
    const lastCall = await saveDirtyPresetWithError(null);

    expect(lastCall).toEqual(["请求失败 (UNKNOWN)", "unknown"]);
  });

  it("字符串抛出值去除首尾空白后保留消息", async () => {
    const lastCall = await saveDirtyPresetWithError("  string boom  ");

    expect(lastCall).toEqual(["string boom (UNKNOWN)", "unknown"]);
  });
});
