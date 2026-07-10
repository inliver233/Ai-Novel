// @vitest-environment jsdom
/**
 * D 类 happy-path 交互测试：EntriesPage 故事条目页核心交互（断言当前正确功能，应绿）。
 *
 * 覆盖：列表加载 / 标签筛选 / 创建条目 / 编辑条目 / 删除条目。
 *
 * 隔离策略（参考 ThemeToggle / ImportPage.stall / AdminUsersPage 测试模式）：
 * - entriesApi CRUD：直接桩，控制列表数据与写操作返回值。
 * - useWizardProgress：隔离向导级联（真实现会并发请求 6 个端点 + 章节列表），提供稳定最小 progress。
 * - useAutoSave：关闭 900ms 自动保存计时器，避免 create/edit 测试中自动保存与手动保存竞争。
 * - toast / confirm：返回 vi.hoisted 稳定引用（useProjectData/saveEntry 的 useCallback deps 含 toast，
 *   若每次 render 返回新对象 → 回调身份变化 → 无限重渲染，见 ImportPage stall 教训）。
 * - window.matchMedia：jsdom 不实现 matchMedia，polyfill 供 useReducedMotion / useIsMobile 使用。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import type { Entry } from "@/types";
import type { WizardProgress } from "@/services/wizard";

type CapturedAutoSave = {
  getSnapshot: () => unknown;
  onSave: (snapshot: unknown) => Promise<void>;
};

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: Deferred<T>["resolve"];
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve;
  });
  return { promise, resolve };
}

// vi.mock 工厂会被提升到文件顶部执行，故用 vi.hoisted 声明被引用的桩，避免 TDZ。
const mocks = vi.hoisted(() => ({
  listEntries: vi.fn(),
  createEntry: vi.fn(),
  updateEntry: vi.fn(),
  deleteEntry: vi.fn(),
  toast: {
    toastSuccess: vi.fn(),
    toastWarning: vi.fn(),
    toastError: vi.fn(),
  } as const,
  confirm: {
    confirm: vi.fn<(opts: unknown) => Promise<boolean>>(),
    choose: vi.fn(),
  },
  wizardRefresh: vi.fn<() => Promise<void>>(),
  wizardBumpLocal: vi.fn(),
  autoSaveOptions: null as CapturedAutoSave | null,
}));

vi.mock("@/services/entriesApi", () => ({
  listEntries: mocks.listEntries,
  createEntry: mocks.createEntry,
  updateEntry: mocks.updateEntry,
  deleteEntry: mocks.deleteEntry,
}));

// 返回 hoisted 的稳定引用，避免无限重渲染。
vi.mock("@/components/ui/toast", () => ({
  useToast: () => mocks.toast,
}));

vi.mock("@/components/ui/confirm", () => ({
  useConfirm: () => mocks.confirm,
}));

// 捕获最近一次配置但不启动计时器，既保证普通测试确定性，也允许并发用例显式触发 autosave。
vi.mock("@/hooks/useAutoSave", () => ({
  useAutoSave: (options: CapturedAutoSave) => {
    mocks.autoSaveOptions = options;
    return { cancel: vi.fn(), flush: vi.fn() };
  },
}));

// 提供稳定的最小向导 progress（nextStep.key="characters" → 触发 EntriesPage 的 primaryAction 分支，
// 但该按钮 accessible name 为 "本页：新增条目"，与 "新增条目" 精确匹配不冲突）。
const fakeProgress: WizardProgress = {
  percent: 40,
  steps: [
    { key: "settings", title: "设置", description: "", href: "/", state: "done" },
    { key: "characters", title: "角色与设定", description: "", href: "/", state: "todo" },
    { key: "outline", title: "大纲", description: "", href: "/", state: "todo" },
  ],
  nextStep: { key: "characters", title: "角色与设定", description: "", href: "/", state: "todo" },
  exportedAt: null,
  writing: { doneChapters: 0, totalChapters: 0 },
};
vi.mock("@/hooks/useWizardProgress", () => ({
  useWizardProgress: () => ({
    loading: false,
    progress: fakeProgress,
    refresh: mocks.wizardRefresh,
    bumpLocal: mocks.wizardBumpLocal,
  }),
}));

import { EntriesPage } from "@/pages/EntriesPage";

const PROJECT_ID = "p1";

function makeEntry(id: string, title: string, content: string, tags: string[]): Entry {
  return {
    id,
    project_id: PROJECT_ID,
    title,
    content,
    tags,
    created_at: "2026-01-01T00:00:00.000Z",
    updated_at: "2026-01-01T00:00:00.000Z",
  };
}

const entryA = makeEntry("e-a", "雨夜相遇", "黑伞男首次出现，留下未解线索", ["设定", "伏笔"]);
const entryB = makeEntry("e-b", "皇城禁令", "情节：禁令颁布后的连锁反应", ["情节"]);

/** jsdom 不实现 matchMedia，polyfill 供 useReducedMotion / useIsMobile 使用。 */
function installMatchMediaPolyfill() {
  if (!window.matchMedia) {
    const impl = (query: string): MediaQueryList =>
      ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList;
    Object.defineProperty(window, "matchMedia", { configurable: true, writable: true, value: impl });
  }
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={[`/projects/${PROJECT_ID}/entries`]}>
      <Routes>
        <Route path="projects/:projectId/entries" element={<EntriesPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

function pageList(items: Entry[]) {
  return { items, next_offset: null };
}

function getTopPrimaryAction(container: HTMLElement): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>("button.btn-primary");
  expect(button).not.toBeNull();
  return button!;
}

function getOpenEntryDialog(): HTMLElement {
  return screen.getByRole("dialog");
}

function getDialogTitleInput(dialog: HTMLElement): HTMLInputElement {
  const input = dialog.querySelector<HTMLInputElement>('input[name="title"]');
  expect(input).not.toBeNull();
  return input!;
}

function getDialogPrimaryAction(dialog: HTMLElement): HTMLButtonElement {
  const button = dialog.querySelector<HTMLButtonElement>("button.btn-primary");
  expect(button).not.toBeNull();
  return button!;
}

describe("EntriesPage 核心 happy-path（D 类，应绿）", () => {
  beforeEach(() => {
    installMatchMediaPolyfill();

    // 重置调用记录与实现，保证用例间独立。
    mocks.listEntries.mockReset();
    mocks.createEntry.mockReset();
    mocks.updateEntry.mockReset();
    mocks.deleteEntry.mockReset();
    mocks.toast.toastSuccess.mockClear();
    mocks.toast.toastWarning.mockClear();
    mocks.toast.toastError.mockClear();
    mocks.confirm.confirm.mockReset().mockResolvedValue(true);
    mocks.wizardRefresh.mockReset().mockResolvedValue(undefined);
    mocks.wizardBumpLocal.mockClear();
    mocks.autoSaveOptions = null;
  });

  it("列表加载后显示已有条目", async () => {
    mocks.listEntries.mockResolvedValue(pageList([entryA, entryB]));

    const { container } = renderPage();

    // 等待挂载 load 完成（listEntries 在 useProjectData loader 内分页拉取，next_offset=null 单页终止）。
    await screen.findByText("雨夜相遇");
    expect(screen.getByText("皇城禁令")).toBeInTheDocument();

    expect(container.querySelectorAll('[role="button"].panel-interactive')).toHaveLength(2);
    expect(mocks.listEntries).toHaveBeenCalledTimes(1);
  });

  it("点击标签筛选后仅显示带该标签的条目", async () => {
    mocks.listEntries.mockResolvedValue(pageList([entryA, entryB]));

    const user = userEvent.setup();
    const { container } = renderPage();
    await screen.findByText("雨夜相遇");

    // 通过测试数据里的标签定位筛选 chip，不绑定生产界面的说明文案。
    const tagButton = Array.from(container.querySelectorAll<HTMLButtonElement>("button.rounded-full")).find(
      (button) => button.textContent === entryA.tags[0],
    );
    expect(tagButton).not.toBeUndefined();
    await user.click(tagButton!);

    await waitFor(() => expect(container.querySelectorAll('[role="button"].panel-interactive')).toHaveLength(1));
    expect(screen.getByText(entryA.title)).toBeInTheDocument();
    expect(screen.queryByText(entryB.title)).toBeNull();
  });

  it("创建条目：填表单提交后新条目出现", async () => {
    // 用 1 条已有条目，避免空态 "新增条目" 按钮与顶部按钮重名。
    mocks.listEntries.mockResolvedValue(pageList([entryA]));
    mocks.createEntry.mockImplementation(
      async (_pid: string, body: { title: string; content?: string; tags?: string[] }) =>
        makeEntry("e-new", body.title, body.content ?? "", body.tags ?? []),
    );

    const user = userEvent.setup();
    const { container } = renderPage();
    await screen.findByText("雨夜相遇");

    // 顶部主操作打开新增抽屉；不锁定按钮的用户可见文案。
    await user.click(getTopPrimaryAction(container));

    const dialog = getOpenEntryDialog();
    const titleInput = getDialogTitleInput(dialog);
    await user.type(titleInput, "测试新条目");

    await user.click(getDialogPrimaryAction(dialog));

    // createEntry 被调用，返回的条目被前置到列表 → 新标题出现。
    await waitFor(() => expect(screen.getByText("测试新条目")).toBeInTheDocument());
    expect(mocks.createEntry).toHaveBeenCalledWith(PROJECT_ID, expect.objectContaining({ title: "测试新条目" }));
    expect(mocks.toast.toastSuccess).toHaveBeenCalledTimes(1);
  });

  it("新建保存进行中再次编辑：首次 POST 完成后合并为对新条目的 PUT", async () => {
    mocks.listEntries.mockResolvedValue(pageList([entryA]));
    const createPending = deferred<Entry>();
    mocks.createEntry.mockReturnValue(createPending.promise);
    mocks.updateEntry.mockImplementation(
      async (entryId: string, body: { title?: string; content?: string; tags?: string[] }) =>
        makeEntry(entryId, body.title ?? "", body.content ?? "", body.tags ?? []),
    );

    const user = userEvent.setup();
    const { container } = renderPage();
    await screen.findByText(entryA.title);
    await user.click(getTopPrimaryAction(container));

    const dialog = getOpenEntryDialog();
    const titleInput = getDialogTitleInput(dialog);
    await user.type(titleInput, "草稿 A");
    await user.click(getDialogPrimaryAction(dialog));
    await waitFor(() => expect(mocks.createEntry).toHaveBeenCalledTimes(1));

    await user.clear(titleInput);
    await user.type(titleInput, "草稿 C");
    const autoSave = mocks.autoSaveOptions;
    expect(autoSave).not.toBeNull();
    await autoSave!.onSave(autoSave!.getSnapshot());
    expect(mocks.updateEntry).not.toHaveBeenCalled();

    await act(async () => {
      createPending.resolve(makeEntry("e-new", "草稿 A", "", []));
      await Promise.resolve();
    });

    await waitFor(() =>
      expect(mocks.updateEntry).toHaveBeenCalledWith("e-new", expect.objectContaining({ title: "草稿 C" })),
    );
    expect(mocks.createEntry).toHaveBeenCalledTimes(1);
  });

  it("切换编辑会话时旧保存完成不能把新快照写回旧条目", async () => {
    mocks.listEntries.mockResolvedValue(pageList([entryA, entryB]));
    const firstUpdatePending = deferred<Entry>();
    mocks.updateEntry
      .mockReturnValueOnce(firstUpdatePending.promise)
      .mockImplementationOnce(async (entryId: string, body: { title?: string; content?: string; tags?: string[] }) =>
        makeEntry(entryId, body.title ?? "", body.content ?? "", body.tags ?? []),
      );

    const user = userEvent.setup();
    renderPage();
    await screen.findByText(entryA.title);

    await user.click(screen.getByText(entryA.title));
    let dialog = getOpenEntryDialog();
    let titleInput = getDialogTitleInput(dialog);
    await user.clear(titleInput);
    await user.type(titleInput, "A 保存中");
    await user.click(getDialogPrimaryAction(dialog));
    await waitFor(() => expect(mocks.updateEntry).toHaveBeenCalledTimes(1));

    await user.click(screen.getByRole("button", { name: "关闭" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await user.click(screen.getByText(entryB.title));

    dialog = getOpenEntryDialog();
    titleInput = getDialogTitleInput(dialog);
    await user.clear(titleInput);
    await user.type(titleInput, "B 最新编辑");
    const autoSave = mocks.autoSaveOptions;
    expect(autoSave).not.toBeNull();
    await autoSave!.onSave(autoSave!.getSnapshot());

    await act(async () => {
      firstUpdatePending.resolve(makeEntry(entryA.id, "A 保存中", entryA.content, entryA.tags));
      await Promise.resolve();
    });

    await waitFor(() =>
      expect(mocks.updateEntry).toHaveBeenNthCalledWith(2, entryB.id, expect.objectContaining({ title: "B 最新编辑" })),
    );
    expect(getDialogTitleInput(getOpenEntryDialog())).toHaveValue("B 最新编辑");
  });

  it("编辑条目：改字段保存后更新反映", async () => {
    mocks.listEntries.mockResolvedValue(pageList([entryA]));
    const updatedTitle = "雨夜相遇（修订）";
    mocks.updateEntry.mockImplementation(
      async (entryId: string, body: { title?: string; content?: string; tags?: string[] }) =>
        makeEntry(entryId, body.title ?? "", body.content ?? "", body.tags ?? []),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByText("雨夜相遇");

    // 点击条目卡片（motion.div role=button）打开编辑抽屉；点击标题文本即冒泡到卡片 onClick=openEdit。
    await user.click(screen.getByText("雨夜相遇"));

    // 抽屉内标题输入框应预填当前标题；清空后输入新标题。
    const dialog = getOpenEntryDialog();
    const titleInput = getDialogTitleInput(dialog);
    expect(titleInput).toHaveValue("雨夜相遇");
    await user.clear(titleInput);
    await user.type(titleInput, updatedTitle);

    await user.click(getDialogPrimaryAction(dialog));

    // updateEntry 被调用，返回的条目替换原条目 → 旧标题消失、新标题出现。
    await waitFor(() => expect(screen.getByText(updatedTitle)).toBeInTheDocument());
    expect(screen.queryByText("雨夜相遇")).toBeNull();
    expect(mocks.updateEntry).toHaveBeenCalledWith(entryA.id, expect.objectContaining({ title: updatedTitle }));
    expect(mocks.toast.toastSuccess).toHaveBeenCalledTimes(1);
  });

  it("删除条目：确认后条目从列表消失", async () => {
    mocks.listEntries.mockResolvedValue(pageList([entryA]));
    mocks.deleteEntry.mockResolvedValue(undefined);

    const user = userEvent.setup();
    renderPage();
    await screen.findByText("雨夜相遇");

    // 通过条目测试数据定位卡片，再点击其 danger action；不锁定按钮文案。
    const entryCard = screen.getByText(entryA.title).closest<HTMLElement>('[role="button"]');
    const deleteButton = entryCard?.querySelector<HTMLButtonElement>("button.text-danger");
    expect(deleteButton).not.toBeNull();
    await user.click(deleteButton!);

    // 确认弹窗（confirm 桩默认 resolve true）→ deleteEntry → setEntries 移除。
    await waitFor(() => expect(screen.queryByText("雨夜相遇")).toBeNull());
    expect(mocks.deleteEntry).toHaveBeenCalledWith(entryA.id);
    expect(mocks.toast.toastSuccess).toHaveBeenCalledTimes(1);
  });
});
