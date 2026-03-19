export const OUTLINE_COPY = {
  loading: "加载中...",
  currentOutline: "当前大纲",
  create: "新建",
  rename: "重命名",
  delete: "删除",
  hasChapters: "该大纲已有章节",
  noChapters: "该大纲暂无章节",
  createChapters: "从大纲创建章节骨架",
  createChaptersDisabledReason: "请先生成包含章节结构的大纲",
  generate: "AI 生成大纲",
  importOutlineFile: "导入本地大纲",
  importingOutlineFile: "导入中...",
  importReadFailed: "读取文件失败，请重试",
  importNoChapters: "未识别到章节结构，请检查 Markdown 标题格式",
  importSaved: "大纲已导入并保存",
  save: "保存大纲",
  saveSuccess: "已保存",
  createdAndSwitched: "已创建并切换大纲",
  renamed: "已重命名",
  deleted: "已删除大纲",
  switched: "已切换大纲",
  chaptersCreatedPrefix: "已创建",
  chaptersCreatedSuffix: "个章节",
  chaptersReplacedPrefix: "已覆盖创建",
  generateDone: "生成完成",
  generateCanceled: "已取消生成",
  generateFailed: "流式生成失败",
  generateFallback: "流式生成失败，已回退非流式",
  generateParseFailed: "流式完成但未收到可用结果，请重试",
  flowTitle: "流程说明",
  flowDescription: "推荐流程：AI 生成大纲 → 预览并应用（覆盖/另存） → 编辑完善 → 从大纲创建章节骨架 → 进入写作。",
  flowHint: "提示：若 “从大纲创建章节骨架” 不可用，请先用 AI 生成大纲并应用（需要解析到章节结构）。",
  editorPlaceholder: "在这里编写大纲（Markdown）...",
  hotkeyHint: "快捷键：Ctrl/Cmd + S 保存",
  titleModalHint: "用于在多个大纲之间切换工作流。",
  titleLabel: "标题",
  titleRequired: "标题不能为空",
  close: "关闭",
  cancel: "取消",
  confirm: "确认",
  generateTitle: "AI 生成大纲",
  generateHint: "生成结果会先预览，可选择覆盖当前大纲或另存为新大纲。",
  generateFormTitle: "基础参数",
  generateFormHint: "先用章节数 / 基调 / 节奏定方向；生成后可在预览里再微调。",
  chapterCount: "章节数",
  chapterCountHint: "可填写长篇目标（如 100/200）；系统会自动压缩每章粒度以尽量覆盖目标章节数。",
  tone: "基调",
  tonePlaceholder: "例如：现实主义，克制但有爆点",
  pacing: "节奏",
  pacingPlaceholder: "例如：前3章强钩子，中段升级，结尾反转",
  advancedTitle: "高级参数",
  advancedHint: "注入世界观/角色卡可让生成更贴近项目设定；流式生成会更快看到输出（偶发会自动回退非流式）。",
  includeWorldSetting: "注入世界观",
  includeCharacters: "注入角色卡",
  stream: "流式生成（beta）",
  streamPreviewTitle: "实时章节预览（JSON）",
  streamPreviewWaiting: "实时章节预览（JSON）：等待首批章节返回...",
  streamRawTitle: "流式原始片段（raw）",
  streamRawWaiting: "流式原始片段（raw）：暂未收到输出，等待当前分段完成...",
  riskHint: "风险提示：生成会调用模型，可能消耗 token 与时间；请先预览再应用（推荐先另存为新大纲）。",
  generateButton: "生成",
  generatingButton: "生成中...",
  cancelGenerate: "取消生成",
  previewTitle: "生成结果预览",
  previewActionHint: "应用方式：覆盖会替换当前大纲并立即保存；另存会创建新大纲并切换（更安全，推荐）。",
  previewCancel: "取消",
  overwriteAndSave: "覆盖当前大纲并保存",
  saveAsNew: "保存为新大纲并切换",
  confirms: {
    deleteOutline: {
      title: "删除当前大纲？",
      description: "将同时删除该大纲下的章节，且不可恢复。",
      confirmText: "删除",
    },
    switchOutline: {
      title: "大纲有未保存修改，是否切换？",
      description: "切换后未保存内容会丢失。",
      confirmText: "保存并切换",
      secondaryText: "不保存切换",
      cancelText: "取消",
    },
    createSkeleton: {
      title: "从大纲创建章节骨架？",
      confirmText: "创建",
    },
    replaceSkeleton: {
      title: "检测到已有章节，是否覆盖？",
      description: "覆盖创建将删除该大纲下所有章节（含正文/摘要），不可恢复。",
      confirmText: "覆盖创建",
    },
    titleModalContinue: {
      title: "当前大纲有未保存修改，是否继续？",
      description: "保存后再切换可保留修改；不保存继续将丢失未保存内容。",
      confirmText: "保存并继续",
      secondaryText: "不保存继续",
      cancelText: "取消",
    },
    overwriteDirty: {
      title: "覆盖当前未保存的大纲？",
      description: "覆盖后将以生成结果替换当前大纲，并立即保存。",
      confirmText: "覆盖并保存",
    },
    saveAsNewDirty: {
      title: "当前大纲有未保存修改，是否继续？",
      description: "保存后再切换可保留修改；不保存继续将丢失未保存内容。",
      confirmText: "保存并继续",
      secondaryText: "不保存继续",
      cancelText: "取消",
    },
    importOutline: {
      title: "导入本地大纲并直接开始写作？",
      description: "将覆盖当前大纲内容并按识别出的章节创建写作骨架。",
      confirmText: "导入并开始写作",
      cancelText: "取消",
    },
  },
} as const;

export function getOutlineTitleModalLabel(mode: "create" | "rename"): string {
  return mode === "create" ? "新建大纲" : "重命名大纲";
}

export function getOutlinePreviewMetaText(chapterCount: number, parseErrorMessage?: string): string {
  return `解析章节：${chapterCount}${parseErrorMessage ? `（${parseErrorMessage}）` : ""}`;
}

export function getOutlineCreateChaptersDescription(chapterCount: number): string {
  return `将根据大纲创建 ${chapterCount} 个章节。`;
}

export function getOutlineCreatedChaptersText(chapterCount: number, replaced = false): string {
  return `${replaced ? OUTLINE_COPY.chaptersReplacedPrefix : OUTLINE_COPY.chaptersCreatedPrefix} ${chapterCount} ${OUTLINE_COPY.chaptersCreatedSuffix}`;
}

export function getOutlineStreamRetryMessage(delayMs: number, retryCount: number, maxRetries: number): string {
  return `流式连接中断，${Math.ceil(delayMs / 1000)} 秒后自动重连（${retryCount}/${maxRetries}）...`;
}
