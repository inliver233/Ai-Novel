// @vitest-environment jsdom
// catalog: frontend-pages#P4
// known_issue：usePromptsPageState.saveAll 的 catch 块对抛出值【不做 instanceof 判定】直接
// 当作 ApiError 解构，导致非 ApiError 抛出（如上游 TypeError / 未规整化的网络失败）时
// err.message / err.code / err.requestId 全为 undefined → toast 文案变成 "boom (undefined)"、
// requestId 被丢弃（无可追溯链路）。
//
// 证据：frontend/src/pages/prompts/usePromptsPageState.ts:451-453
//   } catch (e) {
//     const err = e as ApiError;                                   // ← 裸断言，无 instanceof
//     toast.toastError(`${err.message} (${err.code})`, err.requestId);
//     return false;
//   }
//
// 正确行为：任何抛出值必须先规整化为 ApiError（或安全兜底）再取字段，使 toast 永不出现
// "(undefined)" 子串、且 requestId 不被丢弃。当前实现未规整化 → 本测试【真跑真红】。
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

// 控制位：是否让 PUT /llm_preset 抛出非 ApiError。默认加载期 GET 全成功。
let presetPutShouldFail = false;

function installControlledApiJson() {
  mocks.apiJson.mockImplementation(async (url: string, init?: { method?: string }) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const u = String(url);

    // 触发 bug 的路径：保存时 PUT /llm_preset 抛【非 ApiError】（TypeError）。
    if (method === "PUT" && u.includes("/llm_preset")) {
      if (presetPutShouldFail) throw new TypeError("boom");
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
  mocks.apiJson.mockReset();
  installControlledApiJson();
  mocks.toast.toastSuccess.mockClear();
  mocks.toast.toastWarning.mockClear();
  mocks.toast.toastError.mockClear();
});

describe("usePromptsPageState.saveAll catch 应规整化非 ApiError（frontend-pages#P4 known_issue）", () => {
  it("PUT 抛 TypeError 时，toast 文案不应含 '(undefined)'、requestId 不应被丢弃", { tags: ["@known_issue"] }, async () => {
    const { result } = renderHook(() => usePromptsPageState());

    // 前置：加载完成（6 个 GET 落定），baseline 已置位。
    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    // 制造脏态：把 main 表单 model 改成与 baseline 不同值 → presetDirty 为 true，
    // 使 saveAll 真正发出 PUT /llm_preset（否则 presetDirty 为假会直接 no-op 返回）。
    act(() => {
      result.current.llmPresetPanelProps.setLlmForm((prev: LlmForm) => ({
        ...prev,
        model: "dirty-model",
      }));
    });
    await waitFor(() => {
      expect(result.current.llmPresetPanelProps.presetDirty).toBe(true);
    });

    // 触发 bug：保存时 PUT 抛【非 ApiError】。
    presetPutShouldFail = true;

    // wizardBarProps.onSave 即 saveAll 本体（return 值直接暴露）。
    let ok: boolean | undefined;
    await act(async () => {
      ok = await result.current.wizardBarProps.onSave!();
    });

    // 前置确认：失败路径确已触发——catch 命中、toastError 被调用、saveAll 返回 false。
    // 若此处失败说明测试搭建错误而非 bug。
    expect(ok).toBe(false);
    expect(mocks.toast.toastError).toHaveBeenCalled();

    // frontend-pages#P4 正确行为断言：
    //   1) toast 文案不得等于 "(undefined)"
    //   2) toast 文案不得包含 "(undefined)" 子串
    //   3) requestId 不得被丢弃（应可追溯）
    const lastCall = mocks.toast.toastError.mock.calls.at(-1)!;
    const message = lastCall[0] as string;
    const requestId = lastCall[1];

    expect(message).not.toBe("(undefined)");
    expect(message).not.toContain("(undefined)");
    expect(requestId).not.toBeUndefined();
  });
});
