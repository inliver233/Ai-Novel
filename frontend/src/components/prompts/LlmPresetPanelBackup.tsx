import { useCallback, useMemo } from "react";
import type { Dispatch, ReactNode, SetStateAction } from "react";

import type { LLMProfile, LLMProvider, LLMTaskCatalogItem } from "../../types";
import { describeModelListState, deriveLlmModuleAccessState, type LlmModuleAccessState } from "./llmConnectionState";
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

type ModuleEditorProps = {
  moduleId: string;
  legacyMainFieldNames?: boolean;
  title: string;
  subtitle: string;
  form: LlmForm;
  setForm: (updater: (prev: LlmForm) => LlmForm) => void;
  saving: boolean;
  dirty: boolean;
  capabilities: {
    max_tokens_limit: number | null;
    max_tokens_recommended: number | null;
    context_window_limit: number | null;
  } | null;
  modelList: LlmModelListState;
  modelListHelpText: string;
  headerActions: ReactNode;
};

function RemoteStateNotice(props: { state: LlmModuleAccessState; className?: string }) {
  const toneClass =
    props.state.tone === "success" ? "border-success/30 bg-success/10" : "border-warning/30 bg-warning/10";
  const titleClass = props.state.tone === "success" ? "text-success" : "text-warning";
  return (
    <div className={`rounded-atelier border p-3 ${toneClass}${props.className ? ` ${props.className}` : ""}`}>
      <div className={`text-xs font-medium ${titleClass}`}>{props.state.title}</div>
      <div className="mt-1 text-[11px] text-subtext">{props.state.detail}</div>
    </div>
  );
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

function providerLabel(provider: LLMProvider): string {
  if (provider === "openai") return "OpenAI Chat";
  if (provider === "openai_responses") return "OpenAI Responses";
  if (provider === "openai_compatible") return "OpenAI Compatible Chat";
  if (provider === "openai_responses_compatible") return "OpenAI Compatible Responses";
  if (provider === "anthropic") return "Anthropic";
  return "Gemini";
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

function ModuleEditor(props: ModuleEditorProps) {
  const fieldName = useCallback(
    (key: string) => (props.legacyMainFieldNames ? key : `${props.moduleId}_${key}`),
    [props.legacyMainFieldNames, props.moduleId],
  );
  const extraValidation = useMemo(() => validateExtraJson(props.form.extra), [props.form.extra]);
  const extraErrorText = extraValidation.ok
    ? ""
    : `extra JSON 无效${extraValidation.line ? `（第 ${extraValidation.line} 行，第 ${extraValidation.column ?? 1} 列）` : ""}：${extraValidation.message}`;
  const tokenHint = maxTokensHint(props.capabilities);
  const responsesProvider =
    props.form.provider === "openai_responses" || props.form.provider === "openai_responses_compatible";
  const hasModelOptions = props.modelList.options.length > 0;

  const onFormatExtra = useCallback(() => {
    const parsed = validateExtraJson(props.form.extra);
    if (!parsed.ok) return;
    props.setForm((v) => ({
      ...v,
      extra: JSON.stringify(parsed.value, null, 2),
    }));
  }, [props]);

  return (
    <section className="surface border border-border p-4" aria-label={props.title}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="grid gap-1">
          <div className="text-base font-semibold text-ink">{props.title}</div>
          <div className="text-xs text-subtext">{props.subtitle}</div>
        </div>
        <div className="flex flex-wrap items-center gap-2">{props.headerActions}</div>
      </div>

      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <label className="grid gap-1">
          <span className="text-xs text-subtext">服务商（provider）</span>
          <select
            className="select"
            name={fieldName("provider")}
            value={props.form.provider}
            disabled={props.saving}
            onChange={(e) =>
              props.setForm((v) => ({
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
          <div className="text-[11px] text-subtext">
            当前：{providerLabel(props.form.provider)}。兼容网关通常需要可访问的 `base_url`。
          </div>
        </label>

        <label className="grid gap-1">
          <span className="text-xs text-subtext">模型（model）</span>
          <input
            className="input"
            list={`${props.moduleId}_models`}
            name={fieldName("model")}
            disabled={props.saving}
            value={props.form.model}
            onChange={(e) => props.setForm((v) => ({ ...v, model: e.target.value }))}
          />
          <datalist id={`${props.moduleId}_models`}>
            {props.modelList.options.map((option) => (
              <option key={`${props.moduleId}-${option.id}`} value={option.id}>
                {option.display_name}
              </option>
            ))}
          </datalist>
          {hasModelOptions ? (
            <select
              className="select"
              value={props.form.model}
              disabled={props.saving}
              onChange={(e) => props.setForm((v) => ({ ...v, model: e.target.value }))}
            >
              <option value="">从候选模型中选择…</option>
              {props.modelList.options.map((option) => (
                <option key={`${props.moduleId}-select-${option.id}`} value={option.id}>
                  {option.display_name}
                </option>
              ))}
            </select>
          ) : null}
          <div className="text-[11px] text-subtext">{props.modelListHelpText}</div>
        </label>

        <label className="grid gap-1 md:col-span-2">
          <span className="text-xs text-subtext">接口地址（base_url）</span>
          <input
            className="input"
            disabled={props.saving}
            name={fieldName("base_url")}
            placeholder={
              props.form.provider === "openai_compatible" || props.form.provider === "openai_responses_compatible"
                ? "https://your-gateway.example.com/v1"
                : undefined
            }
            value={props.form.base_url}
            onChange={(e) => props.setForm((v) => ({ ...v, base_url: e.target.value }))}
          />
          <div className="text-[11px] text-subtext">
            OpenAI / OpenAI-compatible 一般包含 `/v1`；Anthropic/Gemini 一般为 host。
          </div>
        </label>
      </div>

      <details className="mt-4 rounded-atelier border border-border/60 bg-canvas px-4 py-3" open={props.dirty}>
        <summary className="cursor-pointer select-none text-sm font-medium text-ink">高级参数与推理配置</summary>
        <div className="mt-3 grid gap-4 md:grid-cols-3">
          <label className="grid gap-1">
            <span className="text-xs text-subtext">temperature</span>
            <input
              className="input"
              value={props.form.temperature}
              onChange={(e) => props.setForm((v) => ({ ...v, temperature: e.target.value }))}
            />
          </label>
          <label className="grid gap-1">
            <span className="text-xs text-subtext">top_p</span>
            <input
              className="input"
              value={props.form.top_p}
              onChange={(e) => props.setForm((v) => ({ ...v, top_p: e.target.value }))}
            />
          </label>
          <label className="grid gap-1">
            <span className="text-xs text-subtext">max_tokens / max_output_tokens</span>
            <input
              className="input"
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
                  value={props.form.presence_penalty}
                  onChange={(e) => props.setForm((v) => ({ ...v, presence_penalty: e.target.value }))}
                />
              </label>
              <label className="grid gap-1">
                <span className="text-xs text-subtext">frequency_penalty</span>
                <input
                  className="input"
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
                value={props.form.top_k}
                onChange={(e) => props.setForm((v) => ({ ...v, top_k: e.target.value }))}
              />
            </label>
          )}

          <label className="grid gap-1 md:col-span-2">
            <span className="text-xs text-subtext">stop（逗号分隔）</span>
            <input
              className="input"
              value={props.form.stop}
              onChange={(e) => props.setForm((v) => ({ ...v, stop: e.target.value }))}
            />
          </label>
          <label className="grid gap-1">
            <span className="text-xs text-subtext">timeout_seconds</span>
            <input
              className="input"
              value={props.form.timeout_seconds}
              onChange={(e) => props.setForm((v) => ({ ...v, timeout_seconds: e.target.value }))}
            />
          </label>

          {(props.form.provider === "openai" || props.form.provider === "openai_compatible" || responsesProvider) && (
            <label className="grid gap-1">
              <span className="text-xs text-subtext">reasoning effort</span>
              <select
                className="select"
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

          {responsesProvider && (
            <label className="grid gap-1">
              <span className="text-xs text-subtext">text verbosity</span>
              <select
                className="select"
                value={props.form.text_verbosity}
                onChange={(e) => props.setForm((v) => ({ ...v, text_verbosity: e.target.value }))}
              >
                <option value="">（默认）</option>
                <option value="low">low</option>
                <option value="medium">medium</option>
                <option value="high">high</option>
              </select>
            </label>
          )}

          {props.form.provider === "anthropic" && (
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
                  placeholder="例如 1024"
                  value={props.form.anthropic_thinking_budget_tokens}
                  onChange={(e) => props.setForm((v) => ({ ...v, anthropic_thinking_budget_tokens: e.target.value }))}
                />
              </label>
            </>
          )}

          {props.form.provider === "gemini" && (
            <>
              <label className="grid gap-1 md:col-span-2">
                <span className="text-xs text-subtext">thinkingConfig.thinkingBudget</span>
                <input
                  className="input"
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
          )}

          <label className="grid gap-1 md:col-span-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-xs text-subtext">extra（JSON，高级扩展）</span>
              <button
                className="btn btn-secondary btn-sm"
                disabled={props.saving || !extraValidation.ok}
                onClick={onFormatExtra}
                type="button"
              >
                一键格式化
              </button>
            </div>
            <textarea
              className="textarea atelier-mono"
              rows={6}
              value={props.form.extra}
              onChange={(e) => props.setForm((v) => ({ ...v, extra: e.target.value }))}
            />
            <div className="text-[11px] text-subtext">
              保留自定义 provider 字段；推理参数建议优先用上面的结构化控件。
            </div>
            {extraErrorText ? <div className="text-xs text-warning">{extraErrorText}</div> : null}
          </label>
        </div>
      </details>
    </section>
  );
}

export function LlmPresetPanelBackup(props: Props) {
  const selectedProfile = props.selectedProfileId
    ? (props.profiles.find((p) => p.id === props.selectedProfileId) ?? null)
    : null;
  const mainAccessState = useMemo(
    () =>
      deriveLlmModuleAccessState({
        scope: "main",
        moduleProvider: props.llmForm.provider,
        selectedProfile,
      }),
    [props.llmForm.provider, selectedProfile],
  );
  const mainModelListHelpText = useMemo(
    () => describeModelListState(props.mainModelList, mainAccessState),
    [mainAccessState, props.mainModelList],
  );
  const taskStates = useMemo(
    () =>
      props.taskModules.map((task) => {
        const boundProfile = task.llm_profile_id ? (props.profiles.find((p) => p.id === task.llm_profile_id) ?? null) : null;
        const taskAccessState = deriveLlmModuleAccessState({
          scope: "task",
          moduleProvider: task.form.provider,
          selectedProfile,
          boundProfile,
        });
        return {
          task,
          boundProfile,
          taskAccessState,
          effectiveProfile: taskAccessState.effectiveProfile,
          taskModelListHelpText: describeModelListState(task.modelList, taskAccessState),
        };
      }),
    [props.profiles, props.taskModules, selectedProfile],
  );
  const readyTaskCount = taskStates.filter((item) => item.taskAccessState.stage === "ready").length;

  return (
    <section className="panel p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="font-content text-xl text-ink">模型编排配置</div>
          <div className="mt-1 text-xs text-subtext">按“先连通，再分配，后调参”组织：降低 provider/Key/回退配置混乱。</div>
        </div>
      </div>

      <div className="surface mt-4 p-4">
        <div className="text-sm font-semibold text-ink">A. API 配置库与连通性</div>
        <div className="mt-1 text-xs text-subtext">先完成 profile、Key 与连通测试，确认可请求远端后再做模块分配。</div>
        <div className="mt-3 grid gap-3 sm:grid-cols-3">
          <label className="grid gap-1 sm:col-span-2">
            <span className="text-xs text-subtext">主模块绑定 profile</span>
            <select
              className="select"
              name="profile_select"
              value={props.selectedProfileId ?? ""}
              disabled={props.profileBusy}
              onChange={(e) => props.onSelectProfile(e.target.value ? e.target.value : null)}
            >
              <option value="">（未绑定后端配置）</option>
              {props.profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} · {p.provider}/{p.model}
                </option>
              ))}
            </select>
          </label>
          <label className="grid gap-1 sm:col-span-1">
            <span className="text-xs text-subtext">新建配置名</span>
            <input
              className="input"
              disabled={props.profileBusy}
              name="profile_name"
              value={props.profileName}
              onChange={(e) => props.onChangeProfileName(e.target.value)}
              placeholder="例如：主网关"
            />
          </label>
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <button className="btn btn-secondary px-3 py-2 text-xs" disabled={props.profileBusy} onClick={props.onCreateProfile} type="button">
            保存为新配置
          </button>
          <button
            className="btn btn-secondary px-3 py-2 text-xs"
            disabled={props.profileBusy || !props.selectedProfileId}
            onClick={props.onUpdateProfile}
            type="button"
          >
            更新当前配置
          </button>
          <button
            className="btn btn-ghost px-3 py-2 text-xs text-accent hover:bg-accent/10"
            disabled={props.profileBusy || !props.selectedProfileId}
            onClick={props.onDeleteProfile}
            type="button"
          >
            删除当前配置
          </button>
        </div>
        <div className="mt-3">
          <RemoteStateNotice state={mainAccessState} />
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            className="btn btn-secondary"
            disabled={props.mainModelList.loading || props.saving || Boolean(mainAccessState.actionReason)}
            onClick={props.onReloadMainModels}
            title={mainAccessState.actionReason ?? undefined}
            type="button"
          >
            {props.mainModelList.loading ? "拉取中…" : "拉取模型列表"}
          </button>
          <button
            className="btn btn-secondary"
            disabled={props.testing || props.profileBusy || Boolean(mainAccessState.actionReason)}
            onClick={props.onTestConnection}
            title={mainAccessState.actionReason ?? undefined}
            type="button"
          >
            {props.testing ? "测试中…" : "测试连接"}
          </button>
        </div>
        <div className="mt-3 grid gap-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-sm text-ink">API Key（后端加密）</span>
            <button
              className="btn btn-secondary btn-sm"
              disabled={!props.selectedProfileId || props.profileBusy || !selectedProfile?.has_api_key}
              onClick={props.onClearApiKey}
              type="button"
            >
              清除 Key
            </button>
          </div>
          <div className="text-xs text-subtext">
            {mainAccessState.stage === "ready"
              ? `已就绪：${selectedProfile?.masked_api_key ?? "（已保存）"}。`
              : mainAccessState.stage === "missing_key"
                ? "已绑定 profile，但还没有保存 Key。"
                : mainAccessState.stage === "missing_profile"
                  ? "请先选择/新建一个 profile，再保存 Key。"
                  : "当前模块 provider 与已绑定 profile 不一致；先统一 provider。"}
          </div>
          <div className="flex gap-2">
            <input
              className="input flex-1"
              placeholder="输入新 Key（不会回显已保存 Key）"
              name="api_key"
              type="password"
              value={props.apiKey}
              onChange={(e) => props.onChangeApiKey(e.target.value)}
            />
            <button
              className="btn btn-primary"
              disabled={!props.selectedProfileId || props.profileBusy || !props.apiKey.trim()}
              onClick={props.onSaveApiKey}
              type="button"
            >
              保存 Key
            </button>
          </div>
        </div>
      </div>

      <div className="surface mt-4 p-4">
        <div className="text-sm font-semibold text-ink">B. 模型参数</div>
        <div className="mt-1 text-xs text-subtext">连接与分配确定后，再调整主模块与任务模块的参数行为。</div>
        <div className="mt-3">
          <ModuleEditor
            moduleId="main-module"
            legacyMainFieldNames
            title="主模块（默认）"
            subtitle="所有未单独覆盖的任务都会使用这里的 provider/model/参数。"
            form={props.llmForm}
            setForm={props.setLlmForm}
            saving={props.saving || props.profileBusy}
            dirty={props.presetDirty}
            capabilities={props.capabilities}
            modelList={props.mainModelList}
            modelListHelpText={mainModelListHelpText}
            headerActions={
              <button className="btn btn-primary" disabled={!props.presetDirty || props.saving} onClick={props.onSave} type="button">
                保存主模块
              </button>
            }
          />
        </div>
        <div className="mt-6 rounded-atelier border border-border/70 bg-canvas p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="grid gap-1">
              <div className="text-sm font-semibold text-ink">任务模块参数覆盖</div>
              <div className="text-xs text-subtext">新增任务模块并调整该任务专属参数，不影响其他流程。</div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <select
                className="select min-w-[240px]"
                value={props.selectedAddTaskKey}
                onChange={(e) => props.onSelectAddTaskKey(e.target.value)}
                disabled={props.addableTasks.length === 0 || props.profileBusy}
              >
                <option value="">选择要新增的任务模块</option>
                {props.addableTasks.map((task) => (
                  <option key={task.key} value={task.key}>
                    [{task.group}] {task.label}
                  </option>
                ))}
              </select>
              <button className="btn btn-primary" disabled={!props.selectedAddTaskKey || props.profileBusy} onClick={props.onAddTaskModule} type="button">
                新增模块
              </button>
            </div>
          </div>
          {taskStates.length === 0 ? (
            <div className="mt-4 rounded-atelier border border-dashed border-border p-4 text-xs text-subtext">
              暂无任务级覆盖。当前所有流程都使用主模块。
            </div>
          ) : (
            <div className="mt-4 grid gap-4">
              {taskStates.map(({ task, taskAccessState, taskModelListHelpText }) => {
                const testing = Boolean(props.taskTesting[task.task_key]);
                const profileBusy = Boolean(props.taskProfileBusy[task.task_key]);
                const taskBusy = task.saving || task.deleting || profileBusy;
                const taskUiLocked = taskBusy || testing;
                return (
                  <div className="rounded-atelier border border-border/70 bg-canvas p-3" key={task.task_key}>
                    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                      <div className="grid gap-1">
                        <div className="text-sm font-semibold text-ink">
                          [{task.group}] {task.label}
                        </div>
                        <div className="text-xs text-subtext">{task.description}</div>
                        <div className="text-[11px] text-subtext">任务键：{task.task_key}</div>
                      </div>
                      <div className="flex flex-wrap items-center gap-2">
                        {task.dirty ? (
                          <span className="rounded-full bg-warning/15 px-2 py-0.5 text-[11px] text-warning">未保存</span>
                        ) : null}
                        <button
                          className="btn btn-secondary btn-sm"
                          disabled={task.modelList.loading || taskUiLocked || Boolean(taskAccessState.actionReason)}
                          onClick={() => props.onReloadTaskModels(task.task_key)}
                          title={taskAccessState.actionReason ?? undefined}
                          type="button"
                        >
                          {task.modelList.loading ? "拉取中…" : "拉取模型列表"}
                        </button>
                        <button
                          className="btn btn-secondary btn-sm"
                          disabled={taskUiLocked || props.profileBusy || Boolean(taskAccessState.actionReason)}
                          onClick={() => props.onTestTaskConnection(task.task_key)}
                          title={taskAccessState.actionReason ?? undefined}
                          type="button"
                        >
                          {testing ? "测试中…" : "测试连接"}
                        </button>
                        <button
                          className="btn btn-primary btn-sm"
                          disabled={!task.dirty || taskUiLocked}
                          onClick={() => props.onSaveTask(task.task_key)}
                          type="button"
                        >
                          {task.saving ? "保存中..." : "保存模块"}
                        </button>
                        <button
                          className="btn btn-ghost btn-sm text-accent hover:bg-accent/10"
                          disabled={taskUiLocked}
                          onClick={() => props.onDeleteTask(task.task_key)}
                          type="button"
                        >
                          {task.deleting ? "删除中..." : "删除模块"}
                        </button>
                      </div>
                    </div>
                    <RemoteStateNotice state={taskAccessState} className="mb-3" />
                    <ModuleEditor
                      moduleId={`task-${task.task_key}`}
                      title="模块参数"
                      subtitle="该任务专属模型参数。"
                      form={task.form}
                      setForm={(updater) => props.onTaskFormChange(task.task_key, updater)}
                      saving={taskUiLocked}
                      dirty={task.dirty}
                      capabilities={null}
                      modelList={task.modelList}
                      modelListHelpText={taskModelListHelpText}
                      headerActions={<></>}
                    />
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      <div className="surface mt-4 p-4">
        <div className="text-sm font-semibold text-ink">C. 使用分配</div>
        <div className="mt-1 text-xs text-subtext">决定每个任务模块使用哪个 profile；留空表示回退到主模块绑定。</div>
        {taskStates.length === 0 ? (
          <div className="mt-3 rounded-atelier border border-dashed border-border p-3 text-xs text-subtext">暂无任务模块覆盖，当前所有流程都会回退到主模块。</div>
        ) : (
          <div className="mt-3 grid gap-2">
            {taskStates.map(({ task, boundProfile, effectiveProfile, taskAccessState }) => (
              <div key={`assign-${task.task_key}`} className="rounded-atelier border border-border/70 bg-canvas p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="text-sm text-ink">
                    [{task.group}] {task.label}
                  </div>
                  <span className="text-[11px] text-subtext">任务键：{task.task_key}</span>
                </div>
                <div className="mt-2 grid gap-2 sm:grid-cols-[minmax(220px,1fr)_2fr]">
                  <select
                    className="select"
                    value={task.llm_profile_id ?? ""}
                    onChange={(e) => props.onTaskProfileChange(task.task_key, e.target.value || null)}
                    disabled={task.saving || task.deleting || Boolean(props.taskProfileBusy[task.task_key])}
                  >
                    <option value="">（回退主配置）</option>
                    {props.profiles.map((profile) => (
                      <option key={`${task.task_key}-${profile.id}`} value={profile.id}>
                        {profile.name} · {profile.provider}/{profile.model}
                      </option>
                    ))}
                  </select>
                  <div className="text-[11px] text-subtext">
                    生效配置：{effectiveProfile ? `${effectiveProfile.name}（${effectiveProfile.provider}/${effectiveProfile.model}）` : "未生效"}
                    {!boundProfile ? "，来源：主配置回退" : "，来源：任务绑定配置"}
                    {effectiveProfile ? (effectiveProfile.has_api_key ? `，Key：${effectiveProfile.masked_api_key ?? "（已保存）"}` : "，尚未保存 Key") : ""}
                    {taskAccessState.actionReason ? `；当前不可用：${taskAccessState.actionReason}` : "；当前可用"}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="surface mt-4 p-4">
        <div className="text-sm font-semibold text-ink">D. 验证与诊断</div>
        <div className="mt-2 grid gap-2 text-xs text-subtext">
          <div>主模块连接状态：{mainAccessState.title}</div>
          <div>主模块模型列表：{mainModelListHelpText}</div>
          <div>
            任务模块可用数：{readyTaskCount}/{taskStates.length}
          </div>
          {taskStates.map(({ task, taskAccessState }) => (
            <div key={`diag-${task.task_key}`}>
              [{task.group}] {task.label}：{taskAccessState.title}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
