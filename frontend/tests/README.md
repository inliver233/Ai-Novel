# 前端测试

前端测试统一收纳在 `frontend/tests/`（与 `backend/tests` 对齐），覆盖纯逻辑、
传输层（apiClient/sseClient）与组件交互。

## 运行

```bash
cd frontend

# 诚实全貌（绿的=对的，红的=现存 bug）
npx vitest run

# 安全网（CI 门禁）：只跑"本该绿"的，红=你引入了新回归=必须修
npx vitest run --coverage --tagsFilter '!@known_issue'

# bug 看板：只跑已知 bug 测试，红=还没修，绿=已修复待毕业（删 tag）
npx vitest run --tagsFilter '@known_issue'

# watch 模式（开发时）
npx vitest

# 覆盖率门禁：statements≥16.5 / branches≥13.7 / functions≥14.2 / lines≥17.57
npx vitest run --coverage --tagsFilter '!@known_issue'

# 类型检查（测试文件夹，CI 也跑）
npx tsc -p tsconfig.test.json --noEmit

# 测试代码 lint/格式与生产构建（CI hard gate）
npx eslint tests vitest.config.ts
npx prettier --check tests vitest.config.ts tsconfig.test.json
npm run build
```

## 目录结构

```
frontend/
├── vitest.config.ts        # @/ 别名 / setupFiles / include tests/ / coverage
├── tsconfig.test.json      # extends app 严格选项 + @/ paths + 测试 types
├── tests/
│   ├── setup.ts            # jest-dom 匹配器 + fetch/Response/SSE 流 mock 工具
│   ├── services/           # 传输层：apiClient / sseClient（流式生成核心）
│   ├── components/         # 组件交互（jsdom + @testing-library/react）
│   ├── lib/ contexts/ pages/ ...   # 从 src/ 迁入的纯逻辑/数据/文案测试
└── package.json            # devDeps: jsdom / @testing-library/* / @vitest/coverage-v8
```

## 两种测试环境

- **node（默认）**：纯逻辑/数据测试更快。`apiClient` / `sseClient` 用 `setup.ts` 的
  `makeJsonResponse` / `makeSseResponse` 打桩 fetch 与 ReadableStream，无需 DOM。
- **jsdom**：组件/交互测试。在文件首行加 `// @vitest-environment jsdom`，即可用
  `@testing-library/react` + `@testing-library/user-event` + jest-dom 匹配器。

## 写新测试

**纯逻辑**（node）：直接 import 被测模块，`vi.fn()` 打桩协作者。

**组件交互**（jsdom）：

```tsx
// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// vi.mock 工厂会被提升，引用外部变量须用 vi.hoisted（否则 TDZ）
const mocks = vi.hoisted(() => ({ onClick: vi.fn() }));
vi.mock("@/some/service", () => mocks);

import { MyComponent } from "@/components/MyComponent";

it("点击触发回调", async () => {
  const user = userEvent.setup();
  render(<MyComponent />);
  await user.click(screen.getByRole("button", { name: /提交/ }));
  expect(mocks.onClick).toHaveBeenCalled();
  expect(screen.getByText("已提交")).toBeInTheDocument();
});
```

## known_issue 工作流（诚实镜像）

前端对已知 bug 采用**诚实镜像**语义（与 `backend/tests` 对齐）——绝不靠 skip 把失败藏成绿色：

- **当前正确行为** → 普通测试（安全网，必须绿）。
- **已知 bug**（实现有缺陷）→ 给 `it`/`test` 传 `{ tags: ["@known_issue"] }`，断言**正确**行为；当前实现有 bug
  故测试**真跑真红**，`npx vitest run` 即可见。`@known_issue` tag 已在 `vitest.config.ts` 注册（strictTags）。三条视图：
  - `npx vitest run`：诚实全貌（安全网绿 + 已知 bug 红）。
  - `npx vitest run --tagsFilter '!@known_issue'`：安全网（CI 门禁，必须全绿 = 可部署）。
  - `npx vitest run --tagsFilter '@known_issue'`：bug 看板（红=待修，绿=已修复）。
- **bug 修复后**：该测试从红变绿 → 删除 `{ tags: ["@known_issue"] }`，让它从"看板"毕业进入"安全网"
  （此后若再红即为真回归）。

```tsx
// @vitest-environment jsdom
import { it, expect } from "vitest";

// M45: ImportPage 停滞检测被 useMemo 冻结，5 分钟无更新应触发停滞提示
it("detects stall after 5min without progress update", { tags: ["@known_issue"] }, async () => {
  // ... 断言【正确行为】：推进 6 分钟后停滞提示应出现
});
```

每个 `known_issue` 测试注释都标注对应的 `项目情况完全分析.md` 问题编号（如 `// M45`）。

## 约定

- 导入统一用 `@/`（指向 `src/`），避免相对路径随文件挪动而脆断（§6.5 / M38）。
- 传输层/状态层测试用 `setup.ts` 的共享 mock 工具，不要每个测试各自手搓 Response。
- 新增组件/页面应补交互测试；断言结构/role/accessible-name，不锁死中文文案快照。
- coverage 基线只升不降；当前安全网门禁为 statements 16.5 / branches 13.7 / functions 14.2 / lines 17.57。
