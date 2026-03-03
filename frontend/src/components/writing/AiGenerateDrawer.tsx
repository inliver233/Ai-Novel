import { useCallback, useEffect, useId, useMemo, useState, type Dispatch, type SetStateAction } from "react";

import { Drawer } from "../ui/Drawer";
import { ProgressBar } from "../ui/ProgressBar";
import { UI_COPY } from "../../lib/uiCopy";
import type { Character, LLMPreset } from "../../types";
import type { GenerateForm } from "./types";
import { ApiError, apiJson } from "../../services/apiClient";

type Props = {
  open: boolean;
  generating: boolean;
  preset: LLMPreset | null;
  projectId?: string;
  activeChapter: boolean;
  dirty: boolean;
  saving?: boolean;
  genForm: GenerateForm;
  setGenForm: Dispatch<SetStateAction<GenerateForm>>;
  characters: Character[];
  streamProgress?: { message: string; progress: number; status: string; charCount?: number } | null;
  onClose: () => void;
  onSave: () => void | Promise<unknown>;
  onSaveAndGenerateNext?: () => void | Promise<unknown>;
  onGenerateAppend: () => void;
  onGenerateReplace: () => void;
  onCancelGenerate?: () => void;
  onOpenPromptInspector: () => void;
  postEditCompareAvailable?: boolean;
  onOpenPostEditCompare?: () => void;
  contentOptimizeCompareAvailable?: boolean;
  onOpenContentOptimizeCompare?: () => void;
};

type WritingStyle = {
  id: string;
  name: string;
  is_preset: boolean;
};

export function AiGenerateDrawer(props: Props) {
  const { onClose, open } = props;
  const streamProviderSupported = !!props.preset && props.preset.provider.startsWith("openai");
  const titleId = useId();
  const advancedPanelId = useId();
  const hasPromptOverride = props.genForm.prompt_override != null;

  const [stylesLoading, setStylesLoading] = useState(false);
  const [presets, setPresets] = useState<WritingStyle[]>([]);
  const [userStyles, setUserStyles] = useState<WritingStyle[]>([]);
  const [projectDefaultStyleId, setProjectDefaultStyleId] = useState<string | null>(null);
  const [stylesError, setStylesError] = useState<ApiError | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const allStyles = useMemo(() => [...presets, ...userStyles], [presets, userStyles]);
  const projectDefaultStyle = useMemo(
    () => allStyles.find((s) => s.id === projectDefaultStyleId) ?? null,
    [allStyles, projectDefaultStyleId],
  );

  const closeDrawer = useCallback(() => {
    setAdvancedOpen(false);
    onClose();
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      closeDrawer();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [closeDrawer, open]);

  useEffect(() => {
    if (!open) return;
    if (!props.projectId) return;
    let cancelled = false;
    Promise.resolve()
      .then(async () => {
        if (cancelled) return null;
        setStylesLoading(true);
        setStylesError(null);
        const [presetRes, userRes, defRes] = await Promise.all([
          apiJson<{ styles: WritingStyle[] }>("/api/writing_styles/presets"),
          apiJson<{ styles: WritingStyle[] }>("/api/writing_styles"),
          apiJson<{ default: { style_id?: string | null } }>(`/api/projects/${props.projectId}/writing_style_default`),
        ]);
        return { presetRes, userRes, defRes };
      })
      .then((res) => {
        if (cancelled || !res) return;
        setPresets(res.presetRes.data.styles ?? []);
        setUserStyles(res.userRes.data.styles ?? []);
        setProjectDefaultStyleId(res.defRes.data.default?.style_id ?? null);
      })
      .catch((e) => {
        if (cancelled) return;
        const err =
          e instanceof ApiError
            ? e
            : new ApiError({ code: "UNKNOWN", message: String(e), requestId: "unknown", status: 0 });
        setStylesError(err);
      })
      .finally(() => {
        if (cancelled) return;
        setStylesLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [open, props.projectId]);

  return (
    <Drawer
      open={open}
      onClose={closeDrawer}
      side="bottom"
      ariaLabelledBy={titleId}
      panelClassName="h-[85vh] w-full overflow-y-auto rounded-atelier border-t border-border bg-canvas p-6 shadow-sm sm:h-full sm:max-w-md sm:rounded-none sm:border-l sm:border-t-0"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="font-content text-2xl text-ink" id={titleId}>
            AI 生成
          </div>
          <div className="mt-1 text-xs text-subtext">
            {props.preset ? `${props.preset.provider} / ${props.preset.model}` : "未加载 LLM 配置"}
          </div>
          {hasPromptOverride ? (
            <div className="mt-2 callout-warning">
              已启用 Prompt 覆盖：生成将使用覆盖文本（可在 Prompt Inspector 回退默认）。
            </div>
          ) : null}
        </div>
        <button className="btn btn-secondary" aria-label="关闭" onClick={closeDrawer} type="button">
          关闭
        </button>
      </div>

      <div className="mt-5 grid gap-4">
        <div className="panel p-3">
          <div className="text-sm font-medium text-ink">基础生成</div>
          <div className="mt-3 grid gap-3">
            <label className="grid gap-1">
              <span className="text-xs text-subtext">用户指令</span>
              <textarea
                className="textarea atelier-content"
                disabled={props.generating}
                name="instruction"
                rows={5}
                value={props.genForm.instruction}
                onChange={(e) => {
                  const value = e.target.value;
                  props.setGenForm((v) => ({ ...v, instruction: value }));
                }}
              />
            </label>

            <label className="grid gap-1">
              <span className="text-xs text-subtext">目标字数（中文按字数=字符数）</span>
              <input
                className="input"
                disabled={props.generating}
                min={100}
                name="target_word_count"
                type="number"
                value={props.genForm.target_word_count ?? ""}
                onChange={(e) => {
                  const next = e.currentTarget.valueAsNumber;
                  props.setGenForm((v) => ({ ...v, target_word_count: Number.isNaN(next) ? null : next }));
                }}
              />
            </label>

            <label className="grid gap-1">
              <span className="text-xs text-subtext">风格</span>
              <select
                className="select"
                disabled={props.generating || stylesLoading}
                name="style_id"
                value={props.genForm.style_id ?? ""}
                onChange={(e) => {
                  const value = e.target.value;
                  props.setGenForm((v) => ({ ...v, style_id: value ? value : null }));
                }}
                aria-label="gen_style_id"
              >
                <option value="">自动（使用项目默认）</option>
                <optgroup label="系统预设">
                  {presets.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </optgroup>
                <optgroup label="我的风格">
                  {userStyles.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </optgroup>
              </select>
              <div className="text-[11px] text-subtext">
                项目默认：{projectDefaultStyle ? projectDefaultStyle.name : "（未设置）"}
                {stylesError ? ` | 加载失败：${stylesError.code}` : ""}
              </div>
            </label>
          </div>
        </div>

        <div className="panel p-3">
          <div className="text-sm font-medium text-ink">记忆注入</div>

          <div className="mt-3">
            <label className="flex items-center justify-between gap-3 text-sm text-ink">
              <span>{UI_COPY.writing.memoryInjectionToggle}</span>
              <input
                className="checkbox"
                checked={props.genForm.memory_injection_enabled}
                disabled={props.generating}
                name="memory_injection_enabled"
                onChange={(e) => {
                  const checked = e.target.checked;
                  props.setGenForm((v) => ({ ...v, memory_injection_enabled: checked }));
                }}
                type="checkbox"
              />
            </label>
            <div className="mt-1 text-[11px] text-subtext">{UI_COPY.writing.memoryInjectionHint}</div>

            {props.genForm.memory_injection_enabled ? (
              <div className="mt-2 rounded-atelier border border-border bg-surface p-3">
                <label className="grid gap-1">
                  <span className="text-xs text-subtext">记忆查询关键词（可选）</span>
                  <input
                    className="input"
                    disabled={props.generating}
                    aria-label="memory_query_text"
                    value={props.genForm.memory_query_text}
                    onChange={(e) => {
                      const value = e.currentTarget.value;
                      props.setGenForm((v) => ({ ...v, memory_query_text: value }));
                    }}
                  />
                </label>
                <div className="mt-1 text-[11px] text-subtext">留空将自动使用“用户指令 + 章节计划”。</div>

                <div className="mt-3 grid gap-2">
                  <div className="text-xs text-subtext">注入模块</div>
                  <div className="text-[11px] text-subtext">会影响本次生成提示词，并同步到「上下文预览」。</div>

                  <label className="flex items-center justify-between gap-3 text-sm text-ink">
                    <span>世界书（worldbook）</span>
                    <input
                      className="checkbox"
                      checked={props.genForm.memory_modules.worldbook}
                      disabled={props.generating}
                      onChange={(e) => {
                        const checked = e.target.checked;
                        props.setGenForm((v) => ({
                          ...v,
                          memory_modules: { ...v.memory_modules, worldbook: checked },
                        }));
                      }}
                      type="checkbox"
                    />
                  </label>

                  <label className="flex items-center justify-between gap-3 text-sm text-ink">
                    <span>表格系统（tables）</span>
                    <input
                      className="checkbox"
                      checked={props.genForm.memory_modules.tables}
                      disabled={props.generating}
                      onChange={(e) => {
                        const checked = e.target.checked;
                        props.setGenForm((v) => ({
                          ...v,
                          memory_modules: { ...v.memory_modules, tables: checked },
                        }));
                      }}
                      type="checkbox"
                    />
                  </label>

                  <details className="rounded-atelier border border-border bg-surface p-2">
                    <summary className="cursor-pointer text-sm text-ink">更多模块（高级）</summary>
                    <div className="mt-2 grid gap-2">
                      <label className="flex items-center justify-between gap-3 text-sm text-ink">
                        <span>剧情记忆（story_memory）</span>
                        <input
                          className="checkbox"
                          checked={props.genForm.memory_modules.story_memory}
                          disabled={props.generating}
                          onChange={(e) => {
                            const checked = e.target.checked;
                            props.setGenForm((v) => ({
                              ...v,
                              memory_modules: { ...v.memory_modules, story_memory: checked },
                            }));
                          }}
                          type="checkbox"
                        />
                      </label>
                      <label className="flex items-center justify-between gap-3 text-sm text-ink">
                        <span>语义历史（semantic_history）</span>
                        <input
                          className="checkbox"
                          checked={props.genForm.memory_modules.semantic_history}
                          disabled={props.generating}
                          onChange={(e) => {
                            const checked = e.target.checked;
                            props.setGenForm((v) => ({
                              ...v,
                              memory_modules: { ...v.memory_modules, semantic_history: checked },
                            }));
                          }}
                          type="checkbox"
                        />
                      </label>
                      <label className="flex items-center justify-between gap-3 text-sm text-ink">
                        <span>未回收伏笔（foreshadow_open_loops）</span>
                        <input
                          className="checkbox"
                          checked={props.genForm.memory_modules.foreshadow_open_loops}
                          disabled={props.generating}
                          onChange={(e) => {
                            const checked = e.target.checked;
                            props.setGenForm((v) => ({
                              ...v,
                              memory_modules: { ...v.memory_modules, foreshadow_open_loops: checked },
                            }));
                          }}
                          type="checkbox"
                        />
                      </label>
                      <label className="flex items-center justify-between gap-3 text-sm text-ink">
                        <span>结构化记忆（structured）</span>
                        <input
                          className="checkbox"
                          checked={props.genForm.memory_modules.structured}
                          disabled={props.generating}
                          onChange={(e) => {
                            const checked = e.target.checked;
                            props.setGenForm((v) => ({
                              ...v,
                              memory_modules: { ...v.memory_modules, structured: checked },
                            }));
                          }}
                          type="checkbox"
                        />
                      </label>
                      <label className="flex items-center justify-between gap-3 text-sm text-ink">
                        <span>向量 RAG（vector_rag）</span>
                        <input
                          className="checkbox"
                          checked={props.genForm.memory_modules.vector_rag}
                          disabled={props.generating}
                          onChange={(e) => {
                            const checked = e.target.checked;
                            props.setGenForm((v) => ({
                              ...v,
                              memory_modules: { ...v.memory_modules, vector_rag: checked },
                            }));
                          }}
                          type="checkbox"
                        />
                      </label>
                      <label className="flex items-center justify-between gap-3 text-sm text-ink">
                        <span>关系图（graph）</span>
                        <input
                          className="checkbox"
                          checked={props.genForm.memory_modules.graph}
                          disabled={props.generating}
                          onChange={(e) => {
                            const checked = e.target.checked;
                            props.setGenForm((v) => ({
                              ...v,
                              memory_modules: { ...v.memory_modules, graph: checked },
                            }));
                          }}
                          type="checkbox"
                        />
                      </label>
                      <label className="flex items-center justify-between gap-3 text-sm text-ink">
                        <span>Fractal（fractal）</span>
                        <input
                          className="checkbox"
                          checked={props.genForm.memory_modules.fractal}
                          disabled={props.generating}
                          onChange={(e) => {
                            const checked = e.target.checked;
                            props.setGenForm((v) => ({
                              ...v,
                              memory_modules: { ...v.memory_modules, fractal: checked },
                            }));
                          }}
                          type="checkbox"
                        />
                      </label>
                    </div>
                  </details>
                </div>
              </div>
            ) : null}
          </div>
        </div>

        {props.genForm.stream && props.generating ? (
          <div className="panel p-3">
            <div className="flex items-center justify-between gap-2 text-xs text-subtext">
              <span className="truncate">{props.streamProgress?.message ?? "连接中..."}</span>
              <span className="shrink-0">{props.streamProgress?.progress ?? 0}%</span>
            </div>
            <ProgressBar ariaLabel="章节流式生成进度" value={props.streamProgress?.progress ?? 0} />
            {props.onCancelGenerate ? (
              <div className="flex justify-end">
                <button className="btn btn-secondary" onClick={props.onCancelGenerate} type="button">
                  取消生成
                </button>
              </div>
            ) : null}
          </div>
        ) : null}

        <div className="panel p-3">
          <div className="text-sm font-medium text-ink">上下文</div>
          <div className="mt-3 grid gap-3">
            <div className="grid gap-2">
              <div className="text-xs text-subtext">上下文注入</div>
              <label className="flex items-center gap-2 text-sm text-ink">
                <input
                  className="checkbox"
                  checked={props.genForm.context.include_world_setting}
                  disabled={props.generating}
                  name="context_include_world_setting"
                  onChange={(e) => {
                    const checked = e.target.checked;
                    props.setGenForm((v) => ({ ...v, context: { ...v.context, include_world_setting: checked } }));
                  }}
                  type="checkbox"
                />
                世界观
              </label>
              <label className="flex items-center gap-2 text-sm text-ink">
                <input
                  className="checkbox"
                  checked={props.genForm.context.include_style_guide}
                  disabled={props.generating}
                  name="context_include_style_guide"
                  onChange={(e) => {
                    const checked = e.target.checked;
                    props.setGenForm((v) => ({ ...v, context: { ...v.context, include_style_guide: checked } }));
                  }}
                  type="checkbox"
                />
                风格
              </label>
              <label className="flex items-center gap-2 text-sm text-ink">
                <input
                  className="checkbox"
                  checked={props.genForm.context.include_constraints}
                  disabled={props.generating}
                  name="context_include_constraints"
                  onChange={(e) => {
                    const checked = e.target.checked;
                    props.setGenForm((v) => ({ ...v, context: { ...v.context, include_constraints: checked } }));
                  }}
                  type="checkbox"
                />
                约束
              </label>
              <label className="flex items-center gap-2 text-sm text-ink">
                <input
                  className="checkbox"
                  checked={props.genForm.context.include_outline}
                  disabled={props.generating}
                  name="context_include_outline"
                  onChange={(e) => {
                    const checked = e.target.checked;
                    props.setGenForm((v) => ({ ...v, context: { ...v.context, include_outline: checked } }));
                  }}
                  type="checkbox"
                />
                大纲
              </label>
              <label className="flex items-center gap-2 text-sm text-ink">
                <input
                  className="checkbox"
                  checked={props.genForm.context.include_smart_context}
                  disabled={props.generating}
                  name="context_include_smart_context"
                  onChange={(e) => {
                    const checked = e.target.checked;
                    props.setGenForm((v) => ({ ...v, context: { ...v.context, include_smart_context: checked } }));
                  }}
                  type="checkbox"
                />
                智能上下文
              </label>
              <label className="flex items-center gap-2 text-sm text-ink">
                <input
                  className="checkbox"
                  checked={props.genForm.context.require_sequential}
                  disabled={props.generating}
                  name="context_require_sequential"
                  onChange={(e) => {
                    const checked = e.target.checked;
                    props.setGenForm((v) => ({ ...v, context: { ...v.context, require_sequential: checked } }));
                  }}
                  type="checkbox"
                />
                严格顺序
              </label>
            </div>

            <label className="grid gap-1">
              <span className="text-xs text-subtext">上一章注入</span>
              <select
                className="select"
                disabled={props.generating}
                name="previous_chapter"
                value={props.genForm.context.previous_chapter}
                onChange={(e) => {
                  const value = e.target.value as GenerateForm["context"]["previous_chapter"];
                  props.setGenForm((v) => ({
                    ...v,
                    context: {
                      ...v.context,
                      previous_chapter: value,
                    },
                  }));
                }}
              >
                <option value="none">不注入</option>
                <option value="tail">结尾（推荐）</option>
                <option value="summary">摘要</option>
                <option value="content">正文</option>
              </select>
              <div className="text-[11px] text-subtext">结尾更利于强衔接，减少开头复述。</div>
            </label>

            <div className="grid gap-2">
              <div className="text-xs text-subtext">注入角色（可选）</div>
              {props.characters.length === 0 ? <div className="text-sm text-subtext">暂无角色</div> : null}
              <div className="max-h-40 overflow-auto rounded-atelier border border-border bg-surface p-2">
                {props.characters.map((c) => (
                  <label key={c.id} className="flex items-center gap-2 px-2 py-1 text-sm text-ink">
                    <input
                      className="checkbox"
                      checked={props.genForm.context.character_ids.includes(c.id)}
                      disabled={props.generating}
                      name={`character_${c.id}`}
                      onChange={(e) => {
                        const checked = e.target.checked;
                        props.setGenForm((v) => {
                          const next = new Set(v.context.character_ids);
                          if (checked) next.add(c.id);
                          else next.delete(c.id);
                          return { ...v, context: { ...v.context, character_ids: Array.from(next) } };
                        });
                      }}
                      type="checkbox"
                    />
                    <span className="truncate">{c.name}</span>
                  </label>
                ))}
              </div>
            </div>
          </div>
        </div>

        <div className="panel p-3">
          <button
            className="ui-focus-ring ui-pressable flex w-full items-center justify-between gap-3 rounded-atelier px-2 py-2 text-left hover:bg-canvas"
            aria-controls={advancedPanelId}
            aria-expanded={advancedOpen}
            onClick={() => setAdvancedOpen((v) => !v)}
            type="button"
          >
            <span className="text-sm font-medium text-ink">高级参数</span>
            <span aria-hidden="true" className="text-xs text-subtext">
              {advancedOpen ? "收起" : "展开"}
            </span>
          </button>

          {!advancedOpen ? (
            <div className="mt-2 text-[11px] text-subtext">默认折叠：流式生成、规划、润色等。</div>
          ) : null}

          {props.preset && props.genForm.stream && !streamProviderSupported ? (
            <div className="mt-2 text-xs text-warning">不支持流式，生成时会自动回退非流式生成</div>
          ) : null}

          {advancedOpen ? (
            <div className="mt-3 grid gap-2" id={advancedPanelId}>
              <label className="flex items-center justify-between gap-3 text-sm text-ink">
                <span>流式生成（beta）</span>
                <input
                  className="checkbox"
                  checked={props.genForm.stream}
                  disabled={props.generating}
                  name="stream"
                  onChange={(e) => {
                    const checked = e.target.checked;
                    props.setGenForm((v) => ({ ...v, stream: checked }));
                  }}
                  type="checkbox"
                />
              </label>

              <label className="flex items-center justify-between gap-3 text-sm text-ink">
                <span>先生成规划</span>
                <input
                  className="checkbox"
                  checked={props.genForm.plan_first}
                  disabled={props.generating}
                  name="plan_first"
                  onChange={(e) => {
                    const checked = e.target.checked;
                    props.setGenForm((v) => ({ ...v, plan_first: checked }));
                  }}
                  type="checkbox"
                />
              </label>

              <label className="flex items-center justify-between gap-3 text-sm text-ink">
                <span>润色</span>
                <input
                  className="checkbox"
                  checked={props.genForm.post_edit}
                  disabled={props.generating}
                  name="post_edit"
                  onChange={(e) => {
                    const checked = e.target.checked;
                    props.setGenForm((v) => ({
                      ...v,
                      post_edit: checked,
                      post_edit_sanitize: checked ? v.post_edit_sanitize : false,
                    }));
                  }}
                  type="checkbox"
                />
              </label>

              <label className="flex items-center justify-between gap-3 text-sm text-ink">
                <span>去味/一致性修复</span>
                <input
                  className="checkbox"
                  checked={props.genForm.post_edit_sanitize}
                  disabled={props.generating || !props.genForm.post_edit}
                  name="post_edit_sanitize"
                  onChange={(e) => {
                    const checked = e.target.checked;
                    props.setGenForm((v) => ({ ...v, post_edit_sanitize: checked }));
                  }}
                  type="checkbox"
                />
              </label>
              <label className="flex items-center justify-between gap-3 text-sm text-ink">
                <span>正文优化</span>
                <input
                  className="checkbox"
                  checked={props.genForm.content_optimize}
                  disabled={props.generating}
                  name="content_optimize"
                  onChange={(e) => {
                    const checked = e.target.checked;
                    props.setGenForm((v) => ({ ...v, content_optimize: checked }));
                  }}
                  type="checkbox"
                />
              </label>
              <div className="text-[11px] text-subtext">失败会降级保留原文，并记录原因。</div>
            </div>
          ) : (
            <div id={advancedPanelId} hidden />
          )}
        </div>

        <div className="panel p-3 text-xs text-subtext">
          生成与编辑内容会自动保存（有短暂延迟），也可随时点击“保存”或 Ctrl/Cmd+S 立即保存。
        </div>
      </div>

      <div className="mt-5 flex flex-wrap justify-end gap-2">
        <button
          className="btn btn-secondary"
          disabled={props.generating || !props.activeChapter}
          onClick={props.onOpenPromptInspector}
          type="button"
        >
          预检/审查{hasPromptOverride ? "（覆盖中）" : ""}
        </button>
        {props.postEditCompareAvailable ? (
          <button
            className="btn btn-secondary"
            disabled={props.generating || !props.onOpenPostEditCompare}
            onClick={() => props.onOpenPostEditCompare?.()}
            type="button"
          >
            润色对比/回退
          </button>
        ) : null}
        {props.contentOptimizeCompareAvailable ? (
          <button
            className="btn btn-secondary"
            disabled={props.generating || !props.onOpenContentOptimizeCompare}
            onClick={() => props.onOpenContentOptimizeCompare?.()}
            type="button"
          >
            正文优化对比/回退
          </button>
        ) : null}
        {hasPromptOverride ? (
          <button
            className="btn btn-secondary"
            disabled={props.generating}
            onClick={() => props.setGenForm((v) => ({ ...v, prompt_override: null }))}
            type="button"
          >
            回退默认
          </button>
        ) : null}
        <button
          className="btn btn-primary"
          disabled={props.generating || !props.activeChapter}
          onClick={props.onGenerateReplace}
          type="button"
        >
          {props.generating ? "生成中..." : "生成"}
        </button>
        {props.onSaveAndGenerateNext ? (
          <button
            className="btn btn-primary"
            disabled={props.generating || props.saving || !props.activeChapter}
            onClick={() => void props.onSaveAndGenerateNext?.()}
            type="button"
          >
            {props.saving ? "保存中..." : "保存并继续"}
          </button>
        ) : null}
        <button
          className="btn btn-secondary"
          disabled={props.generating || !props.activeChapter}
          onClick={props.onGenerateAppend}
          type="button"
        >
          {props.generating ? "生成中..." : "追加生成"}
        </button>
        <button
          className="btn btn-secondary"
          disabled={props.generating || props.saving || !props.activeChapter || !props.dirty}
          onClick={() => void props.onSave()}
          type="button"
        >
          {props.saving ? "保存中..." : "保存"}
        </button>
      </div>
    </Drawer>
  );
}
