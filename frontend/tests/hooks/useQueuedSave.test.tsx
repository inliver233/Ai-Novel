// @vitest-environment jsdom
/**
 * useQueuedSave 并发保存队列（D 类 happy-path，断言当前正确行为，必须绿）。
 *
 * 背景：前端没有独立导出的 useQueuedSave hook。并发保存队列逻辑被【内联复制】在
 * 多个 page state hook 中（settings / prompts / outline / writing ...），其契约一致：
 *   - savingRef 守卫：保存进行中再次触发 → 写入【单槽】队列、立即返回 false
 *   - 单槽合并（merge-latest），【非】排队全部：新触发覆盖槽位，中间触发被丢弃
 *   - finally 排空：进行中的保存结束后，若槽非空，补存【一次】（不按触发数 fan-out）
 *
 * 本文件以脏数据语义最简的 useOutlinePageState（content !== baseline，单一字符串）
 * 为代表，验证该队列模式的当前正确行为。行为语义断言，不锁中文文案。
 *
 * 隔离策略：heavy 依赖（router/toast/confirm/wizard/projectData/autoSave/saveHotkey
 * 及三个 outline 子 hook）全部以 vi.mock 桩化，使 save() → apiJson 成为唯一网络出口；
 * apiJson 对 PUT /outline 走受控 deferred，可在“保存进行中”精确观测调用次数/顺序/参数。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

// ---- vi.mock 工厂会被提升到文件顶部执行，故被引用的桩用 vi.hoisted 声明，避免 TDZ ----
// 注意：被 hook 在每次 render 调用、且其返回值被用作 useEffect 依赖的桩（如 useProjectData
// 的 data），必须返回【引用稳定】的对象——否则每帧新引用 → effect 反复触发 setState → 无限
// 渲染循环 → OOM。故 fixture / wizard 等用模块级单例。
const mocks = vi.hoisted(() => ({
  apiJson: vi.fn(),
  markWizardProjectChanged: vi.fn(),
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
  // 引用稳定的已加载 fixture：content/baseline 初始化为 "baseline-content"，dirty 起始为 false
  projectData: {
    data: {
      outlines: [{ id: "o1", title: "T", has_chapters: false }],
      outline: { id: "o1", title: "T", content_md: "baseline-content", structure: null },
      preset: {},
    },
    setData: vi.fn(),
    loading: false,
    refresh: vi.fn(async () => {}),
  },
  wizard: {
    loading: false,
    progress: { steps: [], nextStep: null },
    refresh: vi.fn(async () => {}),
    bumpLocal: vi.fn(),
  },
}));

vi.mock("@/services/apiClient", async (importOriginal) => {
  const original = (await importOriginal()) as Record<string, unknown>;
  return { ...original, apiJson: mocks.apiJson };
});
vi.mock("@/services/wizard", async (importOriginal) => {
  const original = (await importOriginal()) as Record<string, unknown>;
  return { ...original, markWizardProjectChanged: mocks.markWizardProjectChanged };
});
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
vi.mock("@/hooks/useProjectData", () => ({
  // 直接返回已加载 fixture（引用稳定）：content/baseline 初始化为 "baseline-content"，dirty 起始为 false。
  useProjectData: () => mocks.projectData,
}));
vi.mock("@/hooks/useAutoSave", () => ({
  // 桩化为 no-op controller，避免自动保存定时器干扰手测的并发队列。
  useAutoSave: () => ({ cancel: vi.fn(), flush: vi.fn() }),
}));
vi.mock("@/hooks/useSaveHotkey", () => ({ useSaveHotkey: () => {} }));
vi.mock("@/pages/outline/useDetailedOutlineState", () => ({
  useDetailedOutlineState: () => ({
    items: [],
    selected: null,
    generating: false,
    progress: null,
    skeletonGenerating: false,
    skeletonProgress: null,
    skeletonModalOpen: false,
    generateModalOpen: false,
    refresh: vi.fn(async () => {}),
    generate: vi.fn(async () => true),
    openGenerateModal: vi.fn(),
    cancelGenerate: vi.fn(),
    openSkeletonModal: vi.fn(),
    cancelSkeletonGenerate: vi.fn(),
  }),
}));
vi.mock("@/pages/outline/useOutlineGenerationState", () => ({
  useOutlineGenerationState: () => ({
    open: false,
    generating: false,
    genPreview: null,
    genForm: {},
    setGenForm: vi.fn(),
    streamEnabled: false,
    setStreamEnabled: vi.fn(),
    streamProgress: null,
    streamPreviewJson: null,
    streamRawText: "",
    closeModal: vi.fn(),
    cancelGenerate: vi.fn(),
    generate: vi.fn(async () => true),
    clearPreview: vi.fn(),
    setOpen: vi.fn(),
    overwriteCurrentOutline: vi.fn(async () => true),
    saveAsNewOutline: vi.fn(async () => null),
  }),
}));
vi.mock("@/pages/outline/useOutlineParsingState", () => ({
  useOutlineParsingState: () => ({
    open: false,
    parsing: false,
    openParseModal: vi.fn(),
    parseProgress: null,
    parseForm: {},
    parseResult: null,
    agentCards: [],
    activeTab: "outline",
    closeParseModal: vi.fn(),
    cancelParse: vi.fn(),
    handleContentChange: vi.fn(),
    handleFileUpload: vi.fn(),
    handleAgentConfigChange: vi.fn(),
    startParse: vi.fn(),
    setActiveTab: vi.fn(),
    applyOutline: vi.fn(async () => ({ ok: true })),
    applyDetailedOutlines: vi.fn(async () => true),
    applyCharacters: vi.fn(),
    applyEntries: vi.fn(),
    applyAll: vi.fn(async () => ({ ok: true })),
  }),
}));

import { useOutlinePageState } from "@/pages/outline/useOutlinePageState";

// ---- 受控 apiJson：PUT /outline 返回 pending deferred，便于在“保存进行中”冻结观测 ----
type SaveCall = { url: string; body: { content_md?: string; structure?: unknown } };
const calls: SaveCall[] = [];
const pendingResolvers: Array<() => void> = [];

function installControlledApiJson() {
  mocks.apiJson.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
    const method = (init?.method ?? "GET").toUpperCase();
    if (method === "PUT" && String(url).endsWith("/outline")) {
      const body = init?.body ? (JSON.parse(init.body) as SaveCall["body"]) : {};
      calls.push({ url: String(url), body });
      return new Promise((resolve) => {
        // 回显保存的 content_md，使 baseline 在成功后更新到该值。
        pendingResolvers.push(() =>
          resolve({
            data: { outline: { id: "o1", title: "T", content_md: body.content_md, structure: null } },
            request_id: "r",
          }),
        );
      });
    }
    return { data: {}, request_id: "r" };
  });
}

/** 完成“最早一个”在途保存（resolve 其 deferred），按队列顺序逐个推进。 */
function resolveNextSave() {
  const next = pendingResolvers.shift();
  if (next) next();
}

beforeEach(() => {
  calls.length = 0;
  pendingResolvers.length = 0;
  mocks.apiJson.mockReset();
  installControlledApiJson();
  mocks.toast.toastSuccess.mockClear();
  mocks.toast.toastWarning.mockClear();
  mocks.toast.toastError.mockClear();
});

describe("useQueuedSave 并发保存队列（经 useOutlinePageState.save 暴露）", () => {
  it("空闲态保存：发一次请求、成功后清 dirty、saving 翻转回 false", async () => {
    const { result } = renderHook(() => useOutlinePageState());

    // 初始：content === baseline === "baseline-content"，非脏、非保存中
    expect(result.current.dirty).toBe(false);
    expect(result.current.actionsBarProps.saving).toBe(false);

    // 编辑触发脏态
    act(() => result.current.editorProps.onChange("v1"));
    expect(result.current.dirty).toBe(true);

    // 发起保存（进行中，请求挂起）
    let savePromise!: Promise<boolean>;
    act(() => {
      savePromise = result.current.wizardBarProps.onSave!();
    });
    expect(result.current.actionsBarProps.saving).toBe(true);
    expect(mocks.apiJson).toHaveBeenCalledTimes(1);
    expect(calls[0].body.content_md).toBe("v1");

    // 完成保存
    let ok: boolean | undefined;
    await act(async () => {
      resolveNextSave();
      ok = await savePromise;
    });

    expect(ok).toBe(true);
    expect(result.current.dirty).toBe(false);
    expect(result.current.actionsBarProps.saving).toBe(false);
  });

  it("内容未变更时保存为 no-op：返回 true、不发请求", async () => {
    const { result } = renderHook(() => useOutlinePageState());

    expect(result.current.dirty).toBe(false);

    let ok: boolean | undefined;
    await act(async () => {
      ok = await result.current.wizardBarProps.onSave!();
    });

    expect(ok).toBe(true);
    expect(mocks.apiJson).not.toHaveBeenCalled();
  });

  it(
    "进行中再次触发：单槽必须保存最新编辑而不是旧闭包快照",
    { tags: ["@known_issue"] }, // H39/frontend-pages#5：复制队列已漂移并会丢最新编辑
    async () => {
      const { result } = renderHook(() => useOutlinePageState());

      // 编辑并发起首次保存（挂起、进行中）
      act(() => result.current.editorProps.onChange("A"));
      let firstSave!: Promise<boolean>;
      act(() => {
        firstSave = result.current.wizardBarProps.onSave!();
      });
      await act(async () => {
        await Promise.resolve();
      });

      // 进行中：仅一次请求、saving 为 true
      expect(mocks.apiJson).toHaveBeenCalledTimes(1);
      expect(calls[0].body.content_md).toBe("A");
      expect(result.current.actionsBarProps.saving).toBe(true);

      // 在保存进行中编辑 B 并触发保存，再编辑 C 并触发保存：单槽应 merge-latest，
      // 最终只补存 C。当前实现把 undefined 入槽，finally 又调用旧 save 闭包，补存 A。
      act(() => result.current.editorProps.onChange("B"));
      let secondResult: boolean | undefined;
      let thirdResult: boolean | undefined;
      await act(async () => {
        secondResult = await result.current.wizardBarProps.onSave!();
      });
      act(() => result.current.editorProps.onChange("C"));
      await act(async () => {
        thirdResult = await result.current.wizardBarProps.onSave!();
      });

      expect(secondResult).toBe(false);
      expect(thirdResult).toBe(false);
      // 守卫生效：进行中的请求未被并发打断，仍是那一次
      expect(mocks.apiJson).toHaveBeenCalledTimes(1);

      // 完成首次保存 → finally 排空单槽 → 补存一次（合并语义：3 次触发 → 共 2 次请求，而非 3 次）
      await act(async () => {
        resolveNextSave();
        await firstSave;
      });
      // 排空触发的补存是在途的，再完成它
      await act(async () => {
        resolveNextSave();
      });
      await waitFor(() => expect(result.current.actionsBarProps.saving).toBe(false));

      expect(mocks.apiJson).toHaveBeenCalledTimes(2);
      // 正确行为：A 在途期间的 B 被 C 覆盖，网络写入应为 A → C。
      // 当前 bug：第二次仍是旧闭包里的 A，导致最新编辑未持久化。
      expect(calls.map((c) => c.body.content_md)).toEqual(["A", "C"]);
    },
  );

  it("连续多个保存窗口按发起顺序执行、不丢失不乱序", async () => {
    const { result } = renderHook(() => useOutlinePageState());

    // 窗口 1：保存 A
    act(() => result.current.editorProps.onChange("A"));
    let p1!: Promise<boolean>;
    act(() => {
      p1 = result.current.wizardBarProps.onSave!();
    });
    await act(async () => {
      resolveNextSave();
      await p1;
    });

    // 窗口 2：保存 B
    act(() => result.current.editorProps.onChange("B"));
    let p2!: Promise<boolean>;
    act(() => {
      p2 = result.current.wizardBarProps.onSave!();
    });
    await act(async () => {
      resolveNextSave();
      await p2;
    });

    // 顺序保持 A → B，各一次
    expect(calls.map((c) => c.body.content_md)).toEqual(["A", "B"]);
    expect(result.current.actionsBarProps.saving).toBe(false);
  });

  it("保存失败：返回 false、saving 复位、错误经 toast 上报", async () => {
    // 该用例下 PUT /outline 直接失败
    mocks.apiJson.mockImplementation(async (url: string, init?: { method?: string }) => {
      const method = (init?.method ?? "GET").toUpperCase();
      if (method === "PUT" && String(url).endsWith("/outline")) {
        throw { message: "boom", code: "E1", requestId: "rid-x" };
      }
      return { data: {}, request_id: "r" };
    });

    const { result } = renderHook(() => useOutlinePageState());

    act(() => result.current.editorProps.onChange("Y"));
    let ok: boolean | undefined;
    await act(async () => {
      ok = await result.current.wizardBarProps.onSave!();
    });

    expect(ok).toBe(false);
    expect(mocks.toast.toastError).toHaveBeenCalledWith("boom (E1)", "rid-x");
    expect(result.current.actionsBarProps.saving).toBe(false);
  });
});
