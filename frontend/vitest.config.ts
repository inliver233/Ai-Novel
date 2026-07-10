import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";
import path from "node:path";

const srcDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "src");

export default defineConfig({
  resolve: {
    // `@/` 指向 src/：统一所有测试的导入基址，消除"测试文件挪动即需改相对路径"
    // 的脆性（见 项目情况完全分析.md M38）。
    alias: {
      "@": srcDir,
    },
  },
  // tests/ 不在任何含 `jsx: react-jsx` 的 tsconfig include 内，故显式让 esbuild
  // 使用 automatic JSX runtime，使 .tsx 测试无需 `import React`。
  esbuild: {
    jsx: "automatic",
  },
  test: {
    // 默认 node 环境：纯逻辑/数据测试更快。需要 DOM 的组件/交互测试在文件顶部
    // 用 `// @vitest-environment jsdom` 注释切换到 jsdom（见 tests/setup.ts）。
    environment: "node",
    setupFiles: ["./tests/setup.ts"],
    // 全部前端测试统一收纳在 tests/ 下（与 backend/tests 对齐）。
    include: ["tests/**/*.test.{ts,tsx}"],
    // known_issue tag：前端诚实镜像（与 backend 的 @pytest.mark.known_issue 对齐）。
    // 标记的测试断言【正确行为】、当前实现有 bug 故真跑真红；strictTags 默认开，
    // 故须在此注册 tag。CI 安全网用 `--tagsFilter '!@known_issue'` 排除（必须绿），
    // `--tagsFilter '@known_issue'` 单独查看待修 backlog。
    tags: [{ name: "@known_issue", description: "断言正确行为但当前实现有 bug（失败=待修 backlog）" }],
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      include: ["src/**"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/main.tsx", "src/vite-env.d.ts"],
      // 当前 safety-net 实测基线为 statements 16.50 / branches 13.70 /
      // functions 14.20 / lines 17.57。以报告精度直接建立“只升不降”门禁；
      // 后续新增覆盖时同步上调，不能为了过 CI 下调。
      thresholds: {
        statements: 16.5,
        branches: 13.7,
        functions: 14.2,
        lines: 17.57,
      },
    },
  },
});
