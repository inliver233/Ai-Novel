import { useMemo, useState } from "react";
import type { Dispatch, SetStateAction } from "react";

import { Drawer } from "../ui/Drawer";
import type { LLMProfile, LLMProvider, LLMTaskCatalogItem } from "../../types";
import { deriveLlmModuleAccessState } from "./llmConnectionState";
import type { LlmForm, LlmModelListState } from "./types";

type TaskModuleView = {
  task_key: string;
  label: string;
  group: string;
  description: string;
  llm_profile_id: string | null;
  form: LlmForm;
  dirty: boolean;
  saving: boolean;
  deleting: boolean;
  modelList: LlmModelListState;
};

type Props = {
  llmForm: LlmForm;
  setLlmForm: Dispatch<SetStateAction<LlmForm>>;
  presetDirty: boolean;
  saving: boolean;
  testing: boolean;
  capabilities: {
    max_tokens_limit: number | null;
    max_tokens_recommended: number | null;
    context_window_limit: number | null;
  } | null;
  onTestConnection: () => void;
  onSave: () => void;
  mainModelList: LlmModelListState;
  onReloadMainModels: () => void;

  profiles: LLMProfile[];
  selectedProfileId: string | null;
  onSelectProfile: (profileId: string | null) => void;
  editingProfileId: string | null;
  onSelectEditingProfile: (profileId: string | null) => void;
  onStartCreateProfile: () => void;
  profileEditorForm: LlmForm;
  setProfileEditorForm: Dispatch<SetStateAction<LlmForm>>;
  profileName: string;
  onChangeProfileName: (value: string) => void;
  profileBusy: boolean;
  onCreateProfile: () => void;
  onUpdateProfile: () => void;
  onDeleteProfile: () => void;

  apiKey: string;
  onChangeApiKey: (value: string) => void;
  onSaveApiKey: () => void;
  onClearApiKey: () => void;
  onTestProfileConnection: () => void;
  currentProfileTestResult: {
    ok: boolean;
    message: string;
    latencyMs?: number;
    requestId?: string;
    testedAt: string;
  } | null;
  profileModelCacheMeta: Record<string, { count: number; fetchedAt: string | null }>;

  taskModules: TaskModuleView[];
  addableTasks: LLMTaskCatalogItem[];
  selectedAddTaskKey: string;
  onSelectAddTaskKey: (taskKey: string) => void;
  onAddTaskModule: () => void;
  onTaskProfileChange: (taskKey: string, profileId: string | null) => void;
  onTaskFormChange: (taskKey: string, updater: (prev: LlmForm) => LlmForm) => void;
  taskTesting: Record<string, boolean>;
  onTestTaskConnection: (taskKey: string) => void;
  taskApiKeyDrafts: Record<string, string>;
  onTaskApiKeyDraftChange: (taskKey: string, value: string) => void;
  taskProfileBusy: Record<string, boolean>;
  onSaveTaskApiKey: (taskKey: string) => void;
  onClearTaskApiKey: (taskKey: string) => void;
  onSaveTask: (taskKey: string) => void;
  onDeleteTask: (taskKey: string) => void;
  onReloadTaskModels: (taskKey: string) => void;
};

type ModuleDrawerTarget =
  | { kind: "main" }
  | {
      kind: "task";
      taskKey: string;
    };

type ModuleTableRow =
  | {
      id: string;
      kind: "main";
      label: string;
      group: string;
      description: string;
      profileName: string;
      profileSource: string;
      provider: LLMProvider;
      model: string;
      baseUrl: string;
      dirty: boolean;
      saving: boolean;
      accessTone: "success" | "warning";
      accessText: string;
      deleting: false;
    }
  | {
      id: string;
      kind: "task";
      label: string;
      group: string;
      description: string;
      profileName: string;
      profileSource: string;
      provider: LLMProvider;
      model: string;
      baseUrl: string;
      dirty: boolean;
      saving: boolean;
      accessTone: "success" | "warning";
      accessText: string;
      deleting: boolean;
    };

function providerLabel(provider: LLMProvider): string {
  if (provider === "openai") return "OpenAI Chat";
  if (provider === "openai_responses") return "OpenAI Responses";
  if (provider === "openai_compatible") return "OpenAI Compatible Chat";
  if (provider === "openai_responses_compatible") return "OpenAI Compatible Responses";
  if (provider === "anthropic") return "Anthropic";
  return "Gemini";
}

function providerBadgeClass(provider: LLMProvider): string {
  if (provider === "anthropic") return "border-warning/30 bg-warning/10 text-warning";
  if (provider === "gemini") return "border-success/30 bg-success/10 text-success";
  return "border-border bg-surface text-ink";
}

function maxTokensHint(
  caps: {
    max_tokens_limit: number | null;
    max_tokens_recommended: number | null;
    context_window_limit: number | null;
  } | null,
): string {
  if (!caps) return "";
  const parts: string[] = [];
  if (caps.max_tokens_recommended) parts.push(`推荐 ${caps.max_tokens_recommended}`);
  if (caps.max_tokens_limit) parts.push(`上限 ${caps.max_tokens_limit}`);
  if (caps.context_window_limit) parts.push(`上下文 ${caps.context_window_limit}`);
  return parts.join(" · ");
}

function getJsonParseErrorPosition(message: string): number | null {
  const m = message.match(/\bposition\s+(\d+)\b/i);
  if (!m) return null;
  const pos = Number(m[1]);
  return Number.isFinite(pos) ? pos : null;
}

function getLineAndColumnFromPosition(text: string, position: number): { line: number; column: number } | null {
  if (!Number.isFinite(position) || position < 0 || position > text.length) return null;
  const before = text.slice(0, position);
  const parts = before.split(/\r?\n/);
  const line = parts.length;
  const column = parts[parts.length - 1].length + 1;
  return { line, column };
}

function validateExtraJson(
  raw: string,
): { ok: true; value: unknown } | { ok: false; message: string; position?: number; line?: number; column?: number } {
  const trimmed = (raw ?? "").trim();
  const effective = trimmed ? raw : "{}";
  try {
    return { ok: true, value: JSON.parse(effective) };
  } catch (e) {
    const message = e instanceof Error ? e.message : String(e);
    const position = getJsonParseErrorPosition(message);
    const lc = position !== null ? getLineAndColumnFromPosition(effective, position) : null;
    return {
      ok: false,
      message,
      ...(position !== null ? { position } : {}),
      ...(lc ? lc : {}),
    };
  }
}

function formatTimeText(value: string | null | undefined): string {
  if (!value) return "未拉取";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function StatusPill(props: { tone: "neutral" | "success" | "warning"; children: string }) {
  const cls =
    props.tone === "success"
      ? "border-success/30 bg-success/10 text-success"
      : props.tone === "warning"
        ? "border-warning/30 bg-warning/10 text-warning"
        : "border-border bg-surface text-subtext";
  return <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] ${cls}`}>{props.children}</span>;
}

function ParameterEditor(props: {
  form: LlmForm;
  setForm: (updater: (prev: LlmForm) => LlmForm) => void;
  saving: boolean;
  capabilities: {
    max_tokens_limit: number | null;
    max_tokens_recommended: number | null;
    context_window_limit: number | null;
  } | null;
}) {
  const extraValidation = useMemo(() => validateExtraJson(props.form.extra), [props.form.extra]);
  const extraErrorText = extraValidation.ok
    ? ""
    : `extra JSON 无效${extraValidation.line ? `（第 ${extraValidation.line} 行，第 ${extraValidation.column ?? 1} 列）` : ""}：${extraValidation.message}`;
  const tokenHint = maxTokensHint(props.capabilities);
  const responsesProvider =
    props.form.provider === "openai_responses" || props.form.provider === "openai_responses_compatible";

  return (
    <div className="grid gap-4">
      <div className="grid gap-4 md:grid-cols-3">
        <label className="grid gap-1">
          <span className="text-xs text-subtext">temperature</span>
          <input
            className="input"
            disabled={props.saving}
            value={props.form.temperature}
            onChange={(e) => props.setForm((v) => ({ ...v, temperature: e.target.value }))}
          />
        </label>
        <label className="grid gap-1">
          <span className="text-xs text-subtext">top_p</span>
          <input
            className="input"
            disabled={props.saving}
            value={props.form.top_p}
            onChange={(e) => props.setForm((v) => ({ ...v, top_p: e.target.value }))}
          />
        </label>
        <label className="grid gap-1">
          <span className="text-xs text-subtext">max_tokens / max_output_tokens</span>
          <input
            className="input"
            disabled={props.saving}
            value={props.form.max_tokens}
            onChange={(e) => props.setForm((v) => ({ ...v, max_tokens: e.target.value }))}
          />
          {tokenHint ? <div className="text-[11px] text-subtext">{tokenHint}</div> : null}
        </label>

        {props.form.provider === "openai" || props.form.provider === "openai_compatible" ? (
          <>
            <label className="grid gap-1">
              <span className="text-xs text-subtext">presence_penalty</span>
              <input
                className="input"
                disabled={props.saving}
                value={props.form.presence_penalty}
                onChange={(e) => props.setForm((v) => ({ ...v, presence_penalty: e.target.value }))}
              />
            </label>
            <label className="grid gap-1">
              <span className="text-xs text-subtext">frequency_penalty</span>
              <input
                className="input"
                disabled={props.saving}
                value={props.form.frequency_penalty}
                onChange={(e) => props.setForm((v) => ({ ...v, frequency_penalty: e.target.value }))}
              />
            </label>
          </>
        ) : (
          <label className="grid gap-1">
            <span className="text-xs text-subtext">top_k</span>
            <input
              className="input"
              disabled={props.saving}
              value={props.form.top_k}
              onChange={(e) => props.setForm((v) => ({ ...v, top_k: e.target.value }))}
            />
          </label>
        )}

        <label className="grid gap-1 md:col-span-2">
          <span className="text-xs text-subtext">stop（逗号分隔）</span>
          <input
            className="input"
            disabled={props.saving}
            value={props.form.stop}
            onChange={(e) => props.setForm((v) => ({ ...v, stop: e.target.value }))}
          />
        </label>
        <label className="grid gap-1">
          <span className="text-xs text-subtext">timeout_seconds</span>
          <input
            className="input"
            disabled={props.saving}
            value={props.form.timeout_seconds}
            onChange={(e) => props.setForm((v) => ({ ...v, timeout_seconds: e.target.value }))}
          />
        </label>

        {(props.form.provider === "openai" || props.form.provider === "openai_compatible" || responsesProvider) && (
          <label className="grid gap-1">
            <span className="text-xs text-subtext">reasoning effort</span>
            <select
              className="select"
              disabled={props.saving}
              value={props.form.reasoning_effort}
              onChange={(e) => props.setForm((v) => ({ ...v, reasoning_effort: e.target.value }))}
            >
              <option value="">（默认）</option>
              <option value="minimal">minimal</option>
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
            </select>
          </label>
        )}

        {responsesProvider ? (
          <label className="grid gap-1">
            <span className="text-xs text-subtext">text verbosity</span>
            <select
              className="select"
              disabled={props.saving}
              value={props.form.text_verbosity}
              onChange={(e) => props.setForm((v) => ({ ...v, text_verbosity: e.target.value }))}
            >
              <option value="">（默认）</option>
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
            </select>
          </label>
        ) : null}

        {props.form.provider === "anthropic" ? (
          <>
            <label className="flex items-center gap-2 md:col-span-1">
              <input
                checked={props.form.anthropic_thinking_enabled}
                onChange={(e) => props.setForm((v) => ({ ...v, anthropic_thinking_enabled: e.target.checked }))}
                type="checkbox"
              />
              <span className="text-sm text-ink">启用 thinking</span>
            </label>
            <label className="grid gap-1 md:col-span-2">
              <span className="text-xs text-subtext">thinking.budget_tokens</span>
              <input
                className="input"
                disabled={props.saving}
                placeholder="例如 1024"
                value={props.form.anthropic_thinking_budget_tokens}
                onChange={(e) => props.setForm((v) => ({ ...v, anthropic_thinking_budget_tokens: e.target.value }))}
              />
            </label>
          </>
        ) : null}

        {props.form.provider === "gemini" ? (
          <>
            <label className="grid gap-1 md:col-span-2">
              <span className="text-xs text-subtext">thinkingConfig.thinkingBudget</span>
              <input
                className="input"
                disabled={props.saving}
                placeholder="例如 1024"
                value={props.form.gemini_thinking_budget}
                onChange={(e) => props.setForm((v) => ({ ...v, gemini_thinking_budget: e.target.value }))}
              />
            </label>
            <label className="flex items-center gap-2">
              <input
                checked={props.form.gemini_include_thoughts}
                onChange={(e) => props.setForm((v) => ({ ...v, gemini_include_thoughts: e.target.checked }))}
                type="checkbox"
              />
              <span className="text-sm text-ink">thinkingConfig.includeThoughts</span>
            </label>
          </>
        ) : null}
      </div>

      <label className="grid gap-1">
        <span className="text-xs text-subtext">extra（JSON，高级扩展）</span>
        <textarea
          className="textarea atelier-mono"
          disabled={props.saving}
          rows={7}
          value={props.form.extra}
          onChange={(e) => props.setForm((v) => ({ ...v, extra: e.target.value }))}
        />
        <div className="text-[11px] text-subtext">保留自定义 provider 字段；这里只调行为参数，不处理 API Key 和连通测试。</div>
        {extraErrorText ? <div className="text-xs text-warning">{extraErrorText}</div> : null}
      </label>
    </div>
  );
}

function ApiConfigEditor(props: {
  profiles: LLMProfile[];
  selectedProfileId: string | null;
  editingProfileId: string | null;
  profileBusy: boolean;
  profileEditorForm: LlmForm;
  setProfileEditorForm: Dispatch<SetStateAction<LlmForm>>;
  profileName: string;
  onChangeProfileName: (value: string) => void;
  apiKey: string;
  onChangeApiKey: (value: string) => void;
  onSelectEditingProfile: (profileId: string | null) => void;
  onStartCreateProfile: () => void;
  onSaveProfile: () => void;
  onDeleteProfile: () => void;
  onSaveApiKey: () => void;
  onClearApiKey: () => void;
  onReloadModels: () => void;
  onTestConnection: () => void;
  mainModelList: LlmModelListState;
  currentProfile: LLMProfile | null;
  profileTestResult: Props["currentProfileTestResult"];
  profileModelCacheMeta: Props["profileModelCacheMeta"];
  saving: boolean;
  testing: boolean;
}) {
  const isNewDraft = !props.editingProfileId;
  const canClearKey = Boolean(props.currentProfile?.has_api_key);
  const modelOptions = props.mainModelList.options;
  const modelHint = props.mainModelList.error
    ? props.mainModelList.error
    : props.mainModelList.loading
      ? "正在拉取模型列表…"
      : modelOptions.length
        ? `已固化 ${modelOptions.length} 个候选模型；可下拉选择，也可手动输入 model。`
        : "当前还没有候选模型，可先保存 Key 后点击“拉取模型”。";
  const currentMeta = props.editingProfileId ? props.profileModelCacheMeta[props.editingProfileId] : undefined;

  return (
    <section className="surface p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-ink">A. API 配置中心</div>
          <div className="mt-1 text-xs text-subtext">这里完成 API 配置的完整闭环：新建、编辑、删除、保存 Key、拉取模型、测试连接。</div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button className="btn btn-secondary btn-sm" disabled={props.profileBusy} onClick={props.onStartCreateProfile} type="button">
            新建配置
          </button>
        </div>
      </div>

      <div className="mt-4 grid gap-3 lg:grid-cols-4">
        <button
          className={`rounded-atelier border p-4 text-left transition ${isNewDraft ? "border-accent bg-accent/5" : "border-dashed border-border/70 bg-canvas hover:border-accent/50"}`}
          disabled={props.profileBusy}
          onClick={props.onStartCreateProfile}
          type="button"
        >
          <div className="text-sm font-semibold text-ink">+ 新建 API 配置</div>
          <div className="mt-1 text-xs text-subtext">创建一套新的 provider / base_url / model / Key 配置。</div>
        </button>
        {props.profiles.map((profile) => {
          const meta = props.profileModelCacheMeta[profile.id];
          const active = props.editingProfileId === profile.id;
          const isMainSelected = props.selectedProfileId === profile.id;
          return (
            <button
              key={profile.id}
              className={`rounded-atelier border p-4 text-left transition ${active ? "border-accent bg-accent/5" : "border-border bg-canvas hover:border-accent/50"}`}
              disabled={props.profileBusy}
              onClick={() => props.onSelectEditingProfile(profile.id)}
              type="button"
            >
              <div className="flex flex-wrap items-center gap-2">
                <div className="text-sm font-semibold text-ink">{profile.name}</div>
                {active ? <StatusPill tone="success">当前编辑</StatusPill> : null}
                {isMainSelected ? <StatusPill tone="neutral">主模块使用</StatusPill> : null}
              </div>
              <div className="mt-2 flex flex-wrap gap-2 text-[11px]">
                <span className={`inline-flex rounded-full border px-2 py-0.5 ${providerBadgeClass(profile.provider)}`}>
                  {providerLabel(profile.provider)}
                </span>
                <StatusPill tone={profile.has_api_key ? "success" : "warning"}>
                  {profile.has_api_key ? "Key 已保存" : "缺少 Key"}
                </StatusPill>
              </div>
              <div className="mt-3 text-xs text-subtext">{profile.model}</div>
              <div className="mt-1 line-clamp-1 text-[11px] text-subtext">{profile.base_url || "未填写 base_url"}</div>
              <div className="mt-3 text-[11px] text-subtext">
                候选模型：{meta?.count ?? 0} 个
                <span className="mx-1">·</span>
                最近拉取：{formatTimeText(meta?.fetchedAt)}
              </div>
            </button>
          );
        })}
      </div>

      <div className="mt-4 rounded-atelier border border-border bg-canvas p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-sm font-semibold text-ink">{isNewDraft ? "新建 API 配置" : "编辑 API 配置"}</div>
            <div className="mt-1 text-xs text-subtext">
              这里管理可复用的 API 配置模板。模块实际使用哪一套配置，请到下方“模块配置与使用分配”里选择。
            </div>
          </div>
          {props.currentProfile?.masked_api_key ? (
            <div className="text-xs text-subtext">已保存 Key：{props.currentProfile.masked_api_key}</div>
          ) : null}
        </div>

        <div className="mt-4 grid gap-4 md:grid-cols-2">
          <label className="grid gap-1">
            <span className="text-xs text-subtext">配置名称</span>
            <input
              className="input"
              disabled={props.profileBusy}
              value={props.profileName}
              onChange={(e) => props.onChangeProfileName(e.target.value)}
              placeholder="例如：本地 Ollama / 主网关"
            />
          </label>

          <label className="grid gap-1">
            <span className="text-xs text-subtext">服务商（provider）</span>
            <select
              className="select"
              disabled={props.profileBusy}
              value={props.profileEditorForm.provider}
              onChange={(e) =>
                props.setProfileEditorForm((v) => ({
                  ...v,
                  provider: e.target.value as LLMProvider,
                  max_tokens: "",
                  text_verbosity: "",
                  reasoning_effort: "",
                  anthropic_thinking_enabled: false,
                  anthropic_thinking_budget_tokens: "",
                  gemini_thinking_budget: "",
                  gemini_include_thoughts: false,
                }))
              }
            >
              <option value="openai">openai（官方）</option>
              <option value="openai_responses">openai_responses（官方 /v1/responses）</option>
              <option value="openai_compatible">openai_compatible（中转/本地）</option>
              <option value="openai_responses_compatible">openai_responses_compatible（中转 /v1/responses）</option>
              <option value="anthropic">anthropic（Claude）</option>
              <option value="gemini">gemini</option>
            </select>
          </label>

          <label className="grid gap-1 md:col-span-2">
            <span className="text-xs text-subtext">接口地址（base_url）</span>
            <input
              className="input"
              disabled={props.profileBusy}
              value={props.profileEditorForm.base_url}
              placeholder={
                props.profileEditorForm.provider === "openai_compatible" ||
                props.profileEditorForm.provider === "openai_responses_compatible"
                  ? "http://127.0.0.1:11434/v1"
                  : undefined
              }
              onChange={(e) => props.setProfileEditorForm((v) => ({ ...v, base_url: e.target.value }))}
            />
            <div className="text-[11px] text-subtext">OpenAI / OpenAI-compatible 一般包含 `/v1`；Anthropic/Gemini 一般为 host。</div>
          </label>

          <label className="grid gap-1">
            <span className="text-xs text-subtext">模型（model）</span>
            <input
              className="input"
              list="profile_model_options"
              disabled={props.profileBusy}
              value={props.profileEditorForm.model}
              onChange={(e) => props.setProfileEditorForm((v) => ({ ...v, model: e.target.value }))}
            />
            <datalist id="profile_model_options">
              {modelOptions.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.display_name}
                </option>
              ))}
            </datalist>
            {modelOptions.length ? (
              <select
                className="select"
                disabled={props.profileBusy}
                value={props.profileEditorForm.model}
                onChange={(e) => props.setProfileEditorForm((v) => ({ ...v, model: e.target.value }))}
              >
                <option value="">从候选模型中选择…</option>
                {modelOptions.map((option) => (
                  <option key={`profile-select-${option.id}`} value={option.id}>
                    {option.display_name}
                  </option>
                ))}
              </select>
            ) : null}
            <div className="text-[11px] text-subtext">{modelHint}</div>
          </label>

          <div className="grid gap-1">
            <span className="text-xs text-subtext">固化状态</span>
            <div className="rounded-atelier border border-border bg-surface px-3 py-2 text-sm text-ink">
              当前已固化 {currentMeta?.count ?? 0} 个候选模型
            </div>
            <div className="text-[11px] text-subtext">最近拉取：{formatTimeText(currentMeta?.fetchedAt)}</div>
          </div>
        </div>

        <div className="mt-4 grid gap-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-sm font-medium text-ink">API Key（后端加密）</span>
            <button
              className="btn btn-secondary btn-sm"
              disabled={props.profileBusy || !canClearKey}
              onClick={props.onClearApiKey}
              type="button"
            >
              清除 Key
            </button>
          </div>
          <div className="flex flex-wrap gap-2">
            <input
              className="input min-w-[280px] flex-1"
              disabled={props.profileBusy}
              placeholder="输入新 Key（不会回显已保存 Key）"
              type="password"
              value={props.apiKey}
              onChange={(e) => props.onChangeApiKey(e.target.value)}
            />
            <button className="btn btn-secondary" disabled={props.profileBusy || !props.apiKey.trim()} onClick={props.onSaveApiKey} type="button">
              保存 Key
            </button>
          </div>
        </div>

        <div className="mt-4 flex flex-wrap gap-2">
          <button className="btn btn-primary" disabled={props.profileBusy || props.saving} onClick={props.onSaveProfile} type="button">
            {isNewDraft ? "保存配置" : "更新配置"}
          </button>
          <button
            className="btn btn-secondary"
            disabled={props.profileBusy || props.testing || !props.currentProfile?.has_api_key}
            onClick={props.onReloadModels}
            type="button"
          >
            {props.mainModelList.loading ? "拉取中…" : "拉取模型"}
          </button>
          <button
            className="btn btn-secondary"
            disabled={props.profileBusy || props.testing || !props.currentProfile?.has_api_key}
            onClick={props.onTestConnection}
            type="button"
          >
            {props.testing ? "测试中…" : "测试连接"}
          </button>
          <button
            className="btn btn-ghost text-accent hover:bg-accent/10"
            disabled={props.profileBusy || isNewDraft}
            onClick={props.onDeleteProfile}
            type="button"
          >
            删除配置
          </button>
        </div>

        {props.profileTestResult ? (
          <div className={`mt-4 rounded-atelier border p-3 ${props.profileTestResult.ok ? "border-success/30 bg-success/10" : "border-warning/30 bg-warning/10"}`}>
            <div className={`text-xs font-medium ${props.profileTestResult.ok ? "text-success" : "text-warning"}`}>{props.profileTestResult.ok ? "最近测试成功" : "最近测试失败"}</div>
            <div className="mt-1 text-xs text-subtext">{props.profileTestResult.message}</div>
            <div className="mt-1 text-[11px] text-subtext">
              测试时间：{formatTimeText(props.profileTestResult.testedAt)}
              {props.profileTestResult.requestId ? ` · request_id: ${props.profileTestResult.requestId}` : ""}
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}

export function LlmPresetPanel(props: Props) {
  const [drawerTarget, setDrawerTarget] = useState<ModuleDrawerTarget | null>(null);
  const [showOnlyDirtyModules, setShowOnlyDirtyModules] = useState(false);

  const selectedProfile = props.selectedProfileId
    ? (props.profiles.find((item) => item.id === props.selectedProfileId) ?? null)
    : null;
  const editingProfile = props.editingProfileId
    ? (props.profiles.find((item) => item.id === props.editingProfileId) ?? null)
    : null;

  const moduleRows = useMemo<ModuleTableRow[]>(() => {
    const mainAccessState = deriveLlmModuleAccessState({
      scope: "main",
      moduleProvider: props.llmForm.provider,
      selectedProfile,
    });
    const rows: ModuleTableRow[] = [
      {
        id: "main",
        kind: "main",
        label: "主模块",
        group: "project",
        description: "项目默认模型配置",
        profileName: selectedProfile?.name ?? "未绑定 API 配置",
        profileSource: selectedProfile ? "显式绑定" : "未绑定",
        provider: props.llmForm.provider,
        model: props.llmForm.model,
        baseUrl: props.llmForm.base_url,
        dirty: props.presetDirty,
        saving: props.saving,
        accessTone: mainAccessState.tone,
        accessText: mainAccessState.title,
        deleting: false,
      },
    ];

    for (const task of props.taskModules) {
      const boundProfile = task.llm_profile_id ? (props.profiles.find((item) => item.id === task.llm_profile_id) ?? null) : null;
      const accessState = deriveLlmModuleAccessState({
        scope: "task",
        moduleProvider: task.form.provider,
        selectedProfile,
        boundProfile,
      });
      rows.push({
        id: task.task_key,
        kind: "task",
        label: task.label,
        group: task.group,
        description: task.description,
        profileName: boundProfile?.name ?? selectedProfile?.name ?? "未绑定 API 配置",
        profileSource: boundProfile ? "任务独立绑定" : "继承主模块",
        provider: task.form.provider,
        model: task.form.model,
        baseUrl: task.form.base_url,
        dirty: task.dirty,
        saving: task.saving,
        accessTone: accessState.tone,
        accessText: accessState.title,
        deleting: task.deleting,
      });
    }
    return showOnlyDirtyModules ? rows.filter((row) => row.kind === "main" || row.dirty) : rows;
  }, [props.llmForm, props.presetDirty, props.profiles, props.saving, props.selectedProfileId, props.taskModules, selectedProfile, showOnlyDirtyModules]);

  const currentDrawerTask = drawerTarget?.kind === "task" ? props.taskModules.find((item) => item.task_key === drawerTarget.taskKey) ?? null : null;
  const currentDrawerTaskProfile = currentDrawerTask?.llm_profile_id
    ? (props.profiles.find((item) => item.id === currentDrawerTask.llm_profile_id) ?? null)
    : null;

  const issueCount = moduleRows.filter((row) => row.accessTone !== "success").length;
  const unsavedCount = moduleRows.filter((row) => row.dirty).length;

  return (
    <section className="panel p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="font-content text-xl text-ink">模型编排配置</div>
          <div className="mt-1 text-xs text-subtext">页面上端先管理 API 配置，再通过列表给主模块和任务模块分配使用关系与参数。</div>
        </div>
      </div>

      <ApiConfigEditor
        apiKey={props.apiKey}
        currentProfile={editingProfile}
        profileTestResult={props.currentProfileTestResult}
        mainModelList={props.mainModelList}
        onChangeApiKey={props.onChangeApiKey}
        onChangeProfileName={props.onChangeProfileName}
        onClearApiKey={props.onClearApiKey}
        onDeleteProfile={props.onDeleteProfile}
        onReloadModels={props.onReloadMainModels}
        onSaveApiKey={props.onSaveApiKey}
        onSaveProfile={props.editingProfileId ? props.onUpdateProfile : props.onCreateProfile}
        onSelectEditingProfile={props.onSelectEditingProfile}
        onStartCreateProfile={props.onStartCreateProfile}
        onTestConnection={props.onTestProfileConnection}
        profileBusy={props.profileBusy}
        profileEditorForm={props.profileEditorForm}
        profileModelCacheMeta={props.profileModelCacheMeta}
        profileName={props.profileName}
        profiles={props.profiles}
        saving={props.saving}
        selectedProfileId={props.selectedProfileId}
        setProfileEditorForm={props.setProfileEditorForm}
        testing={props.testing}
        editingProfileId={props.editingProfileId}
      />

      <div className="surface mt-6 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-sm font-semibold text-ink">B. 模块配置与使用分配</div>
            <div className="mt-1 text-xs text-subtext">用列表总览所有模块。点击“编辑”后在抽屉里设置该模块用哪个 API 配置，以及它自己的行为参数。</div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-2 text-xs text-subtext">
              <input checked={showOnlyDirtyModules} onChange={(e) => setShowOnlyDirtyModules(e.target.checked)} type="checkbox" />
              仅显示未保存模块
            </label>
          </div>
        </div>

        <div className="mt-4 flex flex-wrap items-end gap-2 rounded-atelier border border-border bg-canvas p-3">
          <label className="grid min-w-[280px] flex-1 gap-1">
            <span className="text-xs text-subtext">新增任务模块</span>
            <select
              className="select"
              value={props.selectedAddTaskKey}
              onChange={(e) => props.onSelectAddTaskKey(e.target.value)}
            >
              {props.addableTasks.length ? null : <option value="">没有可新增的任务模块</option>}
              {props.addableTasks.map((item) => (
                <option key={item.key} value={item.key}>
                  {item.label} · {item.group}
                </option>
              ))}
            </select>
          </label>
          <button className="btn btn-secondary" disabled={!props.addableTasks.length || !props.selectedAddTaskKey} onClick={props.onAddTaskModule} type="button">
            新增模块
          </button>
        </div>

        <div className="mt-4 overflow-x-auto rounded-atelier border border-border">
          <table className="w-full min-w-[960px] text-left text-sm">
            <thead className="bg-surface text-xs text-subtext">
              <tr>
                <th className="px-3 py-2 font-medium">模块</th>
                <th className="px-3 py-2 font-medium">来源</th>
                <th className="px-3 py-2 font-medium">API 配置</th>
                <th className="px-3 py-2 font-medium">当前模型</th>
                <th className="px-3 py-2 font-medium">状态</th>
                <th className="px-3 py-2 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {moduleRows.map((row) => (
                <tr key={row.id} className="border-t border-border/70 align-top">
                  <td className="px-3 py-3">
                    <div className="font-medium text-ink">{row.label}</div>
                    <div className="mt-1 text-xs text-subtext">{row.description}</div>
                  </td>
                  <td className="px-3 py-3 text-xs text-subtext">
                    <div>{row.group === "project" ? "项目默认" : row.group}</div>
                    <div className="mt-1">{row.profileSource}</div>
                  </td>
                  <td className="px-3 py-3">
                    <div className="text-sm text-ink">{row.profileName}</div>
                    <div className="mt-1 text-xs text-subtext line-clamp-1">{row.baseUrl || "未填写 base_url"}</div>
                  </td>
                  <td className="px-3 py-3">
                    <div className="text-sm text-ink">{row.model || "未设置 model"}</div>
                    <div className="mt-1 text-xs text-subtext">{providerLabel(row.provider)}</div>
                  </td>
                  <td className="px-3 py-3">
                    <div className="flex flex-wrap gap-2">
                      <StatusPill tone={row.accessTone === "success" ? "success" : "warning"}>{row.accessText}</StatusPill>
                      <StatusPill tone={row.dirty ? "warning" : "neutral"}>{row.dirty ? "有未保存修改" : "已保存"}</StatusPill>
                    </div>
                  </td>
                  <td className="px-3 py-3">
                    <div className="flex flex-wrap gap-2">
                      <button
                        className="btn btn-secondary btn-sm"
                        disabled={row.saving}
                        onClick={() => setDrawerTarget(row.kind === "main" ? { kind: "main" } : { kind: "task", taskKey: row.id })}
                        type="button"
                      >
                        编辑
                      </button>
                      {row.kind === "task" ? (
                        <button
                          className="btn btn-ghost btn-sm text-accent hover:bg-accent/10"
                          disabled={row.saving || row.deleting}
                          onClick={() => void props.onDeleteTask(row.id)}
                          type="button"
                        >
                          {row.deleting ? "删除中…" : "删除"}
                        </button>
                      ) : null}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="surface mt-6 p-4">
        <div className="text-sm font-semibold text-ink">C. 诊断</div>
        <div className="mt-1 text-xs text-subtext">汇总当前 API 配置情况、模块保存状态，以及最近一次连接测试结果。</div>
        <div className="mt-4 grid gap-3 md:grid-cols-3">
          <div className="rounded-atelier border border-border bg-canvas p-4">
            <div className="text-xs text-subtext">主模块当前 API 配置</div>
            <div className="mt-2 text-base font-semibold text-ink">{selectedProfile?.name ?? "未绑定"}</div>
            <div className="mt-1 text-xs text-subtext">
              {selectedProfile ? `${selectedProfile.provider} / ${selectedProfile.model}` : "请先在模块抽屉中选择主模块使用的 API 配置"}
            </div>
          </div>
          <div className="rounded-atelier border border-border bg-canvas p-4">
            <div className="text-xs text-subtext">模块状态</div>
            <div className="mt-2 text-base font-semibold text-ink">
              {issueCount} 个异常 / {unsavedCount} 个未保存
            </div>
            <div className="mt-1 text-xs text-subtext">异常通常来自缺少 Key、未绑定 API 配置，或 provider 不匹配。</div>
          </div>
          <div className="rounded-atelier border border-border bg-canvas p-4">
            <div className="text-xs text-subtext">最近测试</div>
            <div className="mt-2 text-sm text-ink">{props.currentProfileTestResult?.message ?? "当前还没有执行过 API 配置测试。"}</div>
            <div className="mt-1 text-[11px] text-subtext">
              {props.currentProfileTestResult?.testedAt ? `时间：${formatTimeText(props.currentProfileTestResult.testedAt)}` : "请在 A 区点击“测试连接”"}
            </div>
          </div>
        </div>
      </div>

      <Drawer
        ariaLabel={drawerTarget?.kind === "main" ? "编辑主模块配置" : "编辑任务模块配置"}
        onClose={() => setDrawerTarget(null)}
        open={Boolean(drawerTarget)}
        panelClassName="h-full w-full max-w-3xl border-l border-border bg-canvas p-6 shadow-2xl"
      >
        {drawerTarget?.kind === "main" ? (
          <div className="grid gap-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="text-lg font-semibold text-ink">主模块</div>
                <div className="mt-1 text-xs text-subtext">主模块决定项目默认使用哪套 API 配置，以及默认的行为参数。</div>
              </div>
              <button className="btn btn-secondary btn-sm" onClick={() => setDrawerTarget(null)} type="button">
                关闭
              </button>
            </div>

            <section className="rounded-atelier border border-border bg-surface p-4">
              <div className="text-sm font-semibold text-ink">使用哪个 API 配置</div>
              <div className="mt-1 text-xs text-subtext">这里选择主模块默认使用的 API 配置模板；切换时只同步连接字段，不覆盖温度等参数。</div>
              <label className="mt-3 grid gap-1">
                <span className="text-xs text-subtext">主模块 API 配置</span>
                <select
                  className="select"
                  disabled={props.profileBusy}
                  value={props.selectedProfileId ?? ""}
                  onChange={(e) => props.onSelectProfile(e.target.value ? e.target.value : null)}
                >
                  <option value="">（未绑定）</option>
                  {props.profiles.map((profile) => (
                    <option key={profile.id} value={profile.id}>
                      {profile.name} · {profile.provider}/{profile.model}
                    </option>
                  ))}
                </select>
              </label>
              <div className="mt-3 rounded-atelier border border-border bg-canvas px-3 py-2 text-xs text-subtext">
                当前连接：{props.llmForm.provider} / {props.llmForm.model}
                <div className="mt-1 line-clamp-1">{props.llmForm.base_url || "未填写 base_url"}</div>
              </div>
            </section>

            <section className="rounded-atelier border border-border bg-surface p-4">
              <div className="text-sm font-semibold text-ink">行为参数</div>
              <div className="mt-1 text-xs text-subtext">这里只调主模块自己的行为参数；Key、拉模型、连通测试统一在 A 区处理。</div>
              <div className="mt-4">
                <ParameterEditor capabilities={props.capabilities} form={props.llmForm} saving={props.saving} setForm={props.setLlmForm} />
              </div>
            </section>

            <div className="flex flex-wrap gap-2">
              <button className="btn btn-primary" disabled={props.saving} onClick={props.onSave} type="button">
                {props.saving ? "保存中…" : "保存主模块"}
              </button>
              <button className="btn btn-secondary" onClick={() => setDrawerTarget(null)} type="button">
                返回列表
              </button>
            </div>
          </div>
        ) : currentDrawerTask ? (
          <div className="grid gap-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="text-lg font-semibold text-ink">{currentDrawerTask.label}</div>
                <div className="mt-1 text-xs text-subtext">{currentDrawerTask.description}</div>
              </div>
              <button className="btn btn-secondary btn-sm" onClick={() => setDrawerTarget(null)} type="button">
                关闭
              </button>
            </div>

            <section className="rounded-atelier border border-border bg-surface p-4">
              <div className="text-sm font-semibold text-ink">使用哪个 API 配置</div>
              <div className="mt-1 text-xs text-subtext">任务模块可独立绑定 API 配置；留空则继承主模块当前的连接字段。</div>
              <label className="mt-3 grid gap-1">
                <span className="text-xs text-subtext">任务模块 API 配置</span>
                <select
                  className="select"
                  disabled={currentDrawerTask.saving}
                  value={currentDrawerTask.llm_profile_id ?? ""}
                  onChange={(e) => props.onTaskProfileChange(currentDrawerTask.task_key, e.target.value ? e.target.value : null)}
                >
                  <option value="">继承主模块 API 配置</option>
                  {props.profiles.map((profile) => (
                    <option key={profile.id} value={profile.id}>
                      {profile.name} · {profile.provider}/{profile.model}
                    </option>
                  ))}
                </select>
              </label>
              <div className="mt-3 rounded-atelier border border-border bg-canvas px-3 py-2 text-xs text-subtext">
                当前连接：{currentDrawerTask.form.provider} / {currentDrawerTask.form.model}
                <div className="mt-1">{currentDrawerTaskProfile?.name ?? selectedProfile?.name ?? "继承主模块（当前未绑定）"}</div>
                <div className="mt-1 line-clamp-1">{currentDrawerTask.form.base_url || "未填写 base_url"}</div>
              </div>
            </section>

            <section className="rounded-atelier border border-border bg-surface p-4">
              <div className="text-sm font-semibold text-ink">行为参数</div>
              <div className="mt-1 text-xs text-subtext">这里单独调这个任务模块的行为参数，不会影响其他模块。</div>
              <div className="mt-4">
                <ParameterEditor
                  capabilities={props.capabilities}
                  form={currentDrawerTask.form}
                  saving={currentDrawerTask.saving}
                  setForm={(updater) => props.onTaskFormChange(currentDrawerTask.task_key, updater)}
                />
              </div>
            </section>

            <div className="flex flex-wrap gap-2">
              <button
                className="btn btn-primary"
                disabled={currentDrawerTask.saving}
                onClick={() => props.onSaveTask(currentDrawerTask.task_key)}
                type="button"
              >
                {currentDrawerTask.saving ? "保存中…" : "保存任务模块"}
              </button>
              <button
                className="btn btn-ghost text-accent hover:bg-accent/10"
                disabled={currentDrawerTask.saving || currentDrawerTask.deleting}
                onClick={() => props.onDeleteTask(currentDrawerTask.task_key)}
                type="button"
              >
                {currentDrawerTask.deleting ? "删除中…" : "删除任务模块"}
              </button>
            </div>
          </div>
        ) : null}
      </Drawer>
    </section>
  );
}
