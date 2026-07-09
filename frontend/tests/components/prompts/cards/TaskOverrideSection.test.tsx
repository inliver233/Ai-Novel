import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DEFAULT_LLM_FORM } from "@/pages/prompts/models";
import type { LlmForm } from "@/components/prompts/types";
import { TaskOverrideSection } from "@/components/prompts/cards/TaskOverrideSection";

function buildTaskOverrideHtml(form: LlmForm): string {
  return renderToStaticMarkup(
    <TaskOverrideSection
      addableTasks={[]}
      llmForm={DEFAULT_LLM_FORM}
      onAddTaskModule={() => undefined}
      onClearTaskApiKey={() => undefined}
      onDeleteTask={() => undefined}
      onReloadTaskModels={() => undefined}
      onSaveTask={() => undefined}
      onSaveTaskApiKey={() => undefined}
      onSelectAddTaskKey={() => undefined}
      onTaskApiKeyDraftChange={() => undefined}
      onTaskFormChange={() => undefined}
      onTaskProfileChange={() => undefined}
      onTestTaskConnection={() => undefined}
      profiles={[
        {
          id: "profile-1",
          owner_user_id: "user-1",
          name: "主配置",
          provider: form.provider,
          base_url: form.base_url,
          model: form.model,
          temperature: 0.7,
          top_p: 1,
          max_tokens: 12000,
          presence_penalty: 0,
          frequency_penalty: 0,
          top_k: null,
          stop: [],
          timeout_seconds: 180,
          extra: {},
          has_api_key: true,
          masked_api_key: "sk-***",
          created_at: "2026-03-26T00:00:00Z",
          updated_at: "2026-03-26T00:00:00Z",
        },
      ]}
      selectedAddTaskKey=""
      selectedProfile={{
        id: "profile-1",
        owner_user_id: "user-1",
        name: "主配置",
        provider: form.provider,
        base_url: form.base_url,
        model: form.model,
        temperature: 0.7,
        top_p: 1,
        max_tokens: 12000,
        presence_penalty: 0,
        frequency_penalty: 0,
        top_k: null,
        stop: [],
        timeout_seconds: 180,
        extra: {},
        has_api_key: true,
        masked_api_key: "sk-***",
        created_at: "2026-03-26T00:00:00Z",
        updated_at: "2026-03-26T00:00:00Z",
      }}
      taskApiKeyDrafts={{}}
      taskModules={[
        {
          task_key: "task-1",
          label: "测试任务",
          group: "draft",
          description: "测试任务模块",
          llm_profile_id: null,
          form,
          dirty: false,
          saving: false,
          deleting: false,
          modelList: { loading: false, options: [], warning: null, error: null, requestId: null },
        },
      ]}
      taskProfileBusy={{}}
      taskTesting={{}}
    />,
  );
}

describe("TaskOverrideSection", () => {
  it("renders compact advanced fields for OpenAI task overrides", () => {
    const html = buildTaskOverrideHtml({
      ...DEFAULT_LLM_FORM,
      provider: "openai",
      extra: '{"foo":"bar"}',
    });

    expect(html).toContain("更多参数");
    expect(html).toContain("top_p");
    expect(html).toContain("presence_penalty");
    expect(html).toContain("frequency_penalty");
    expect(html).toContain("stop（逗号分隔）");
    expect(html).toContain("reasoning_effort");
    expect(html).toContain("extra（JSON，高级扩展）");
  });

  it("renders anthropic thinking controls in compact editor", () => {
    const html = buildTaskOverrideHtml({
      ...DEFAULT_LLM_FORM,
      provider: "anthropic",
      anthropic_thinking_enabled: true,
      anthropic_thinking_budget_tokens: "1024",
    });

    expect(html).toContain("启用 thinking");
    expect(html).toContain("thinking.budget_tokens");
    expect(html).toContain("top_k");
  });

  it("renders gemini thinking controls in compact editor", () => {
    const html = buildTaskOverrideHtml({
      ...DEFAULT_LLM_FORM,
      provider: "gemini",
      gemini_thinking_budget: "2048",
      gemini_include_thoughts: true,
    });

    expect(html).toContain("thinkingConfig.thinkingBudget");
    expect(html).toContain("thinkingConfig.includeThoughts");
    expect(html).toContain("top_k");
  });
});
