export type OutlinePromiseSourceKind = "hook" | "foreshadow" | "both";

export type OutlinePromiseItem = {
  title: string;
  content: string;
  source_kind: OutlinePromiseSourceKind;
  planted_chapter_number: number;
  planned_chapter_numbers: number[];
  resolution_hint: string;
};

export type OutlineGenChapter = {
  number: number;
  title: string;
  beats: string[];
  highlights: string[];
  promise_items: OutlinePromiseItem[];
};

export type OutlineGenResult = {
  outline_md: string;
  chapters: OutlineGenChapter[];
  raw_output: string;
  parse_error?: { code: string; message: string };
};

const CORE_PLAN_SECTION_LABELS = new Set(["主要内容", "本章要点", "章节要点", "核心要点", "剧情要点"]);
const HIGHLIGHT_SECTION_LABELS = new Set(["爽点"]);
const HOOK_SECTION_LABELS = new Set(["钩子"]);
const FORESHADOW_SECTION_LABELS = new Set(["伏笔"]);
const PLACEHOLDER_TEXTS = new Set(["主要内容", "本章要点", "章节要点", "核心要点", "剧情要点", "内容"]);

type OutlineChapterStoredShape = {
  number?: unknown;
  title?: unknown;
  beats?: unknown;
  highlights?: unknown;
  promise_items?: unknown;
  hooks?: unknown;
  foreshadows?: unknown;
};

type RawChapterBlock = {
  number: number;
  title: string;
  lines: string[];
};

type SectionKey = "core" | "highlight" | "hook" | "foreshadow" | "other";

type ParsedSectionLine = {
  section: SectionKey;
  inlineText: string;
};

function normalizeWhitespace(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function removeListPrefix(value: string): string {
  return value.replace(/^(?:[-*+]\s*|\d+[.)、]\s*)/, "");
}

function normalizeLineText(value: string): string {
  return normalizeWhitespace(removeListPrefix(value));
}

function sanitizeBeatText(value: string): string {
  const text = normalizeLineText(value);
  if (!text) return "";
  if (PLACEHOLDER_TEXTS.has(text)) return "";
  if (/^(?:埋下章|回收|小回收|大回收)\s*[:：]?/.test(text)) return "";
  return text;
}

function sanitizeLooseTexts(values: string[]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const raw of values ?? []) {
    const text = normalizeLineText(String(raw ?? ""));
    if (!text || PLACEHOLDER_TEXTS.has(text)) continue;
    if (seen.has(text)) continue;
    seen.add(text);
    out.push(text);
  }
  return out;
}

export function sanitizeChapterBeats(beats: string[]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const raw of beats ?? []) {
    const text = sanitizeBeatText(String(raw ?? ""));
    if (!text) continue;
    if (seen.has(text)) continue;
    seen.add(text);
    out.push(text);
    if (out.length >= 16) break;
  }
  return out;
}

function parseChineseNumeralToInt(value: string): number | null {
  const normalized = value.replaceAll("两", "二").replaceAll("〇", "零").trim();
  if (!normalized) return null;
  if (/^\d+$/.test(normalized)) return Number(normalized);
  const digits: Record<string, number> = {
    零: 0,
    一: 1,
    二: 2,
    三: 3,
    四: 4,
    五: 5,
    六: 6,
    七: 7,
    八: 8,
    九: 9,
  };
  const units: Record<string, number> = {
    十: 10,
    百: 100,
    千: 1000,
  };
  let result = 0;
  let current = 0;
  for (const char of normalized) {
    if (char in digits) {
      current = digits[char] ?? 0;
      continue;
    }
    if (char in units) {
      const unit = units[char] ?? 1;
      const base = current === 0 ? 1 : current;
      result += base * unit;
      current = 0;
      continue;
    }
    return null;
  }
  result += current;
  return result > 0 ? result : null;
}

function cleanChapterTitle(raw: string, fallback: string): string {
  const compact = normalizeWhitespace(raw);
  return compact || fallback;
}

function extractHeadingChapter(line: string, fallbackNumber: number): { number: number; title: string } | null {
  const heading = line.match(/^#{1,6}\s*(.+)$/);
  if (!heading) return null;
  const text = heading[1]?.trim() ?? "";
  if (!text) return null;
  const zh = text.match(/^第\s*([0-9零一二三四五六七八九十百千两〇]+)\s*[卷章节回部篇集]\s*[:：\-、.\s]+(.*)$/);
  if (zh) {
    const parsed = parseChineseNumeralToInt(zh[1] ?? "");
    return {
      number: parsed ?? fallbackNumber,
      title: cleanChapterTitle(zh[2] ?? "", text),
    };
  }
  const en = text.match(/^(?:chapter|chap)\s*([0-9]+)\s*[:：\-、.\s]*(.*)$/i);
  if (en) {
    return {
      number: Number(en[1] ?? fallbackNumber),
      title: cleanChapterTitle(en[2] ?? "", text),
    };
  }
  return null;
}

function extractPlainChapter(line: string, fallbackNumber: number): { number: number; title: string } | null {
  const text = line.trim();
  if (!text) return null;
  const zh = text.match(
    /^(?:[-*+]\s*|\d+[.)、]\s*)?第\s*([0-9零一二三四五六七八九十百千两〇]+)\s*[卷章节回部篇集]\s*[:：\-、.\s]+(.*)$/,
  );
  if (zh) {
    const parsed = parseChineseNumeralToInt(zh[1] ?? "");
    return {
      number: parsed ?? fallbackNumber,
      title: cleanChapterTitle(zh[2] ?? "", text),
    };
  }
  const en = text.match(/^(?:[-*+]\s*|\d+[.)、]\s*)?(?:chapter|chap)\s*([0-9]+)\s*[:：\-、.\s]*(.*)$/i);
  if (en) {
    return {
      number: Number(en[1] ?? fallbackNumber),
      title: cleanChapterTitle(en[2] ?? "", text),
    };
  }
  return null;
}

function classifySectionLabel(label: string): SectionKey | null {
  if (!label) return null;
  if (CORE_PLAN_SECTION_LABELS.has(label)) return "core";
  if (HIGHLIGHT_SECTION_LABELS.has(label)) return "highlight";
  if (HOOK_SECTION_LABELS.has(label)) return "hook";
  if (FORESHADOW_SECTION_LABELS.has(label)) return "foreshadow";
  return null;
}

function parseSectionLine(line: string): ParsedSectionLine | null {
  const normalized = normalizeWhitespace(removeListPrefix(line).replace(/^#{1,6}\s*/, ""));
  if (!normalized) return null;

  const direct = classifySectionLabel(normalized.replace(/[：:]\s*$/, ""));
  if (direct) {
    return {
      section: direct,
      inlineText: "",
    };
  }

  const inlineMatch = normalized.match(/^([^：:]+)[：:]\s*(.+)$/);
  if (!inlineMatch) return null;
  const section = classifySectionLabel(normalizeWhitespace(inlineMatch[1] ?? ""));
  if (!section) return null;
  return {
    section,
    inlineText: normalizeWhitespace(inlineMatch[2] ?? ""),
  };
}

function splitParagraphs(lines: string[]): string[][] {
  const paragraphs: string[][] = [];
  let current: string[] = [];
  for (const rawLine of lines) {
    const text = normalizeLineText(rawLine);
    if (!text) {
      if (current.length > 0) {
        paragraphs.push(current);
        current = [];
      }
      continue;
    }
    current.push(text);
  }
  if (current.length > 0) paragraphs.push(current);
  return paragraphs;
}

function parseChapterNumberReferences(text: string): number[] {
  const refs: number[] = [];
  const seen = new Set<number>();
  const regex = /第\s*([0-9零一二三四五六七八九十百千两〇]+)\s*章/g;
  for (const match of text.matchAll(regex)) {
    const parsed = parseChineseNumeralToInt(match[1] ?? "");
    if (!parsed || seen.has(parsed)) continue;
    seen.add(parsed);
    refs.push(parsed);
  }
  return refs;
}

function truncateTitle(text: string, maxLength = 80): string {
  const value = normalizeWhitespace(text);
  if (value.length <= maxLength) return value;
  return `${value.slice(0, maxLength).trimEnd()}…`;
}

function buildLegacyPromiseItems(rawHooks: unknown, rawForeshadows: unknown, fallbackChapterNumber: number): OutlinePromiseItem[] {
  const items: OutlinePromiseItem[] = [];
  if (Array.isArray(rawHooks)) {
    for (const raw of rawHooks) {
      const text = normalizeLineText(String(raw ?? ""));
      if (!text) continue;
      items.push({
        title: truncateTitle(text),
        content: text,
        source_kind: "hook",
        planted_chapter_number: fallbackChapterNumber,
        planned_chapter_numbers: [],
        resolution_hint: "",
      });
    }
  }
  if (Array.isArray(rawForeshadows)) {
    for (const raw of rawForeshadows) {
      if (typeof raw === "string") {
        const text = normalizeLineText(raw);
        if (!text) continue;
        items.push({
          title: truncateTitle(text),
          content: "",
          source_kind: "foreshadow",
          planted_chapter_number: fallbackChapterNumber,
          planned_chapter_numbers: [],
          resolution_hint: "",
        });
        continue;
      }
      if (!raw || typeof raw !== "object") continue;
      const item = normalizePromiseItem(raw as Partial<OutlinePromiseItem>, fallbackChapterNumber);
      if (item) items.push(item);
    }
  }
  return items;
}

function normalizePromiseItem(raw: Partial<OutlinePromiseItem>, fallbackChapterNumber: number): OutlinePromiseItem | null {
  const title = normalizeLineText(String(raw.title ?? ""));
  const content = normalizeWhitespace(String(raw.content ?? ""));
  const sourceKindRaw = String(raw.source_kind ?? "foreshadow").trim().toLowerCase();
  const source_kind: OutlinePromiseSourceKind =
    sourceKindRaw === "hook" || sourceKindRaw === "both" ? (sourceKindRaw as OutlinePromiseSourceKind) : "foreshadow";
  const plantedRaw = Number(raw.planted_chapter_number ?? fallbackChapterNumber);
  const planted_chapter_number = Number.isFinite(plantedRaw) && plantedRaw > 0 ? plantedRaw : fallbackChapterNumber;
  const planned_chapter_numbers = Array.isArray(raw.planned_chapter_numbers)
    ? Array.from(
        new Set(
          raw.planned_chapter_numbers
            .map((item) => Number(item))
            .filter((item) => Number.isFinite(item) && item > 0)
            .map((item) => Math.floor(item)),
        ),
      )
    : [];
  const resolution_hint = normalizeWhitespace(String(raw.resolution_hint ?? ""));
  if (!title && !content) return null;
  return {
    title: title || truncateTitle(content),
    content,
    source_kind,
    planted_chapter_number,
    planned_chapter_numbers,
    resolution_hint,
  };
}

function mergePromiseSourceKinds(
  left: OutlinePromiseSourceKind,
  right: OutlinePromiseSourceKind,
): OutlinePromiseSourceKind {
  if (left === right) return left;
  if (left === "both" || right === "both") return "both";
  return "both";
}

function sanitizePromiseItems(items: OutlinePromiseItem[], fallbackChapterNumber: number): OutlinePromiseItem[] {
  const out: OutlinePromiseItem[] = [];
  const indexByKey = new Map<string, number>();
  for (const raw of items ?? []) {
    const item = normalizePromiseItem(raw, fallbackChapterNumber);
    if (!item) continue;
    const buildKey = (semanticText: string) =>
      [
        semanticText,
        item.planted_chapter_number,
        item.resolution_hint,
        item.planned_chapter_numbers.join(","),
      ].join("||");
    const candidateKeys = Array.from(
      new Set([
        buildKey(normalizeLineText(item.content || item.title)),
        buildKey(normalizeLineText(item.title || item.content)),
      ]),
    ).filter(Boolean);
    const existingIndex = candidateKeys
      .map((key) => indexByKey.get(key))
      .find((value): value is number => value !== undefined);
    if (existingIndex !== undefined) {
      const existing = out[existingIndex];
      if (existing) {
        existing.source_kind = mergePromiseSourceKinds(existing.source_kind, item.source_kind);
      }
      continue;
    }
    for (const key of candidateKeys) {
      indexByKey.set(key, out.length);
    }
    out.push(item);
  }
  return out;
}

function normalizeChapter(chapter: OutlineGenChapter): OutlineGenChapter {
  return {
    number: chapter.number,
    title: chapter.title || `第${chapter.number}章`,
    beats: sanitizeChapterBeats(chapter.beats ?? []),
    highlights: sanitizeLooseTexts(chapter.highlights ?? []),
    promise_items: sanitizePromiseItems(chapter.promise_items ?? [], chapter.number),
  };
}

export function extractOutlineChapters(structure: unknown): OutlineGenChapter[] {
  if (!structure || typeof structure !== "object") return [];
  const maybe = structure as { chapters?: unknown };
  if (!Array.isArray(maybe.chapters)) return [];
  return maybe.chapters
    .map((item) => {
      const raw = item as OutlineChapterStoredShape;
      const number = typeof raw.number === "number" ? raw.number : Number(raw.number);
      if (!Number.isFinite(number) || number <= 0) return null;
      const title = typeof raw.title === "string" ? raw.title : "";
      const beats = Array.isArray(raw.beats) ? raw.beats.map((value) => String(value)) : [];
      const highlights = Array.isArray(raw.highlights) ? raw.highlights.map((value) => String(value)) : [];
      const promiseItemsRaw = Array.isArray(raw.promise_items)
        ? raw.promise_items
        : buildLegacyPromiseItems(raw.hooks, raw.foreshadows, number);
      const promise_items = Array.isArray(promiseItemsRaw)
        ? promiseItemsRaw
            .map((value) => (typeof value === "object" && value ? normalizePromiseItem(value as Partial<OutlinePromiseItem>, number) : null))
            .filter((value): value is OutlinePromiseItem => Boolean(value))
        : [];
      return normalizeChapter({
        number,
        title,
        beats,
        highlights,
        promise_items,
      });
    })
    .filter((value): value is OutlineGenChapter => Boolean(value));
}

export function normalizeOutlineGenResult(raw: unknown, fallbackRawOutput = ""): OutlineGenResult | null {
  if (!raw || typeof raw !== "object") return null;
  const data = raw as {
    outline_md?: unknown;
    chapters?: unknown;
    raw_output?: unknown;
    parse_error?: unknown;
  };
  const outline_md = typeof data.outline_md === "string" ? data.outline_md : "";
  const chapters = extractOutlineChapters({ chapters: data.chapters });
  const raw_output = typeof data.raw_output === "string" ? data.raw_output : fallbackRawOutput;
  const parse_error =
    data.parse_error && typeof data.parse_error === "object"
      ? {
          code: String((data.parse_error as { code?: unknown }).code ?? ""),
          message: String((data.parse_error as { message?: unknown }).message ?? ""),
        }
      : undefined;
  if (!outline_md && chapters.length === 0 && !raw_output) return null;
  return { outline_md, chapters, raw_output, parse_error };
}

export function parseOutlineGenResultFromText(text: string): OutlineGenResult | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const candidates: string[] = [trimmed];
  const firstBrace = trimmed.indexOf("{");
  const lastBrace = trimmed.lastIndexOf("}");
  if (firstBrace >= 0 && lastBrace > firstBrace) {
    candidates.push(trimmed.slice(firstBrace, lastBrace + 1));
  }
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate) as unknown;
      const normalized = normalizeOutlineGenResult(parsed, text);
      if (normalized) return normalized;
    } catch {
      // ignore and continue fallback parsing
    }
  }
  return null;
}

function parseMainBeats(lines: string[]): string[] {
  const out: string[] = [];
  for (const rawLine of lines) {
    const text = sanitizeBeatText(rawLine);
    if (!text) continue;
    out.push(text);
  }
  return sanitizeChapterBeats(out);
}

function parseHighlights(lines: string[]): string[] {
  return sanitizeLooseTexts(
    lines
      .map((rawLine) => normalizeLineText(rawLine))
      .filter(Boolean),
  );
}

function parseHookPromises(lines: string[], chapterNumber: number): OutlinePromiseItem[] {
  return splitParagraphs(lines)
    .map((paragraph) => normalizeWhitespace(paragraph.join(" ")))
    .filter(Boolean)
    .map((text) => ({
      title: truncateTitle(text),
      content: text,
      source_kind: "hook" as const,
      planted_chapter_number: chapterNumber,
      planned_chapter_numbers: [],
      resolution_hint: "",
    }));
}

function parseForeshadowPromises(lines: string[], chapterNumber: number): OutlinePromiseItem[] {
  const items: OutlinePromiseItem[] = [];
  let current:
    | {
        title: string;
        detailLines: string[];
        plannedChapterNumbers: number[];
        resolutionHint: string;
        plantedChapterNumber: number;
      }
    | null = null;
  let hadBlank = false;
  let sawMeta = false;

  const pushCurrent = () => {
    if (!current) return;
    const title = normalizeLineText(current.title);
    const content = normalizeWhitespace(current.detailLines.join(" "));
    if (!title && !content && !current.resolutionHint) {
      current = null;
      return;
    }
    items.push({
      title: title || truncateTitle(content || current.resolutionHint),
      content,
      source_kind: "foreshadow",
      planted_chapter_number: current.plantedChapterNumber,
      planned_chapter_numbers: current.plannedChapterNumbers,
      resolution_hint: current.resolutionHint,
    });
    current = null;
    hadBlank = false;
    sawMeta = false;
  };

  for (const rawLine of lines) {
    const trimmed = rawLine.trim();
    if (!trimmed) {
      hadBlank = true;
      continue;
    }
    const text = normalizeLineText(trimmed);
    if (!text) continue;

    const plantedMatch = text.match(/^埋下章\s*[:：]\s*(.+)$/);
    if (plantedMatch) {
      if (!current) {
        current = {
          title: "",
          detailLines: [],
          plannedChapterNumbers: [],
          resolutionHint: "",
          plantedChapterNumber: chapterNumber,
        };
      }
      const parsed = parseChineseNumeralToInt(plantedMatch[1] ?? "");
      if (parsed) current.plantedChapterNumber = parsed;
      sawMeta = true;
      hadBlank = false;
      continue;
    }

    const resolutionMatch = text.match(/^回收\s*[:：]\s*(.+)$/);
    if (resolutionMatch) {
      if (!current) {
        current = {
          title: "",
          detailLines: [],
          plannedChapterNumbers: [],
          resolutionHint: "",
          plantedChapterNumber: chapterNumber,
        };
      }
      const hint = normalizeWhitespace(resolutionMatch[1] ?? "");
      current.resolutionHint = current.resolutionHint ? `${current.resolutionHint}；${hint}` : hint;
      current.plannedChapterNumbers = Array.from(
        new Set([...current.plannedChapterNumbers, ...parseChapterNumberReferences(hint)]),
      );
      sawMeta = true;
      hadBlank = false;
      continue;
    }

    if (!current) {
      current = {
        title: text,
        detailLines: [],
        plannedChapterNumbers: [],
        resolutionHint: "",
        plantedChapterNumber: chapterNumber,
      };
      hadBlank = false;
      sawMeta = false;
      continue;
    }

    if (!current.title) {
      current.title = text;
      hadBlank = false;
      sawMeta = false;
      continue;
    }

    if (hadBlank || sawMeta) {
      pushCurrent();
      current = {
        title: text,
        detailLines: [],
        plannedChapterNumbers: [],
        resolutionHint: "",
        plantedChapterNumber: chapterNumber,
      };
      continue;
    }

    current.detailLines.push(text);
    hadBlank = false;
    sawMeta = false;
  }

  pushCurrent();
  return sanitizePromiseItems(items, chapterNumber);
}

function scanMarkdownChapterBlocks(markdown: string): RawChapterBlock[] {
  const lines = markdown.split(/\r?\n/);
  const blocks: RawChapterBlock[] = [];
  let current: RawChapterBlock | null = null;
  let fallbackNumber = 1;

  const pushCurrent = () => {
    if (!current) return;
    blocks.push(current);
    current = null;
    fallbackNumber = blocks.length + 1;
  };

  for (const rawLine of lines) {
    const line = rawLine.trim();
    const heading = line ? extractHeadingChapter(line, fallbackNumber) : null;
    if (heading) {
      pushCurrent();
      current = { number: heading.number, title: heading.title, lines: [] };
      continue;
    }
    const plain = line ? extractPlainChapter(line, fallbackNumber) : null;
    if (plain) {
      pushCurrent();
      current = { number: plain.number, title: plain.title, lines: [] };
      continue;
    }
    if (current) current.lines.push(rawLine);
  }

  pushCurrent();
  return blocks;
}

function parseMarkdownChapterBlock(block: RawChapterBlock): OutlineGenChapter {
  const sections: Record<SectionKey, string[]> = {
    core: [],
    highlight: [],
    hook: [],
    foreshadow: [],
    other: [],
  };
  let currentSection: SectionKey = "core";

  for (const rawLine of block.lines) {
    const line = rawLine.trim();
    const sectionLine = line ? parseSectionLine(line) : null;
    const label = sectionLine?.section ?? null;
    if (label) {
      currentSection = label;
      if (sectionLine?.inlineText) {
        sections[currentSection].push(sectionLine.inlineText);
      }
      continue;
    }
    sections[currentSection].push(rawLine);
  }

  return normalizeChapter({
    number: block.number,
    title: block.title,
    beats: parseMainBeats(sections.core),
    highlights: parseHighlights(sections.highlight),
    promise_items: [
      ...parseHookPromises(sections.hook, block.number),
      ...parseForeshadowPromises(sections.foreshadow, block.number),
    ],
  });
}

export function buildChapterPlanFromBeats(beats: string[]): string {
  return sanitizeChapterBeats(beats).join("；");
}

function formatPromisePlanLine(item: OutlinePromiseItem): string {
  const base = item.source_kind === "hook" ? item.content || item.title : item.title;
  const parts = [base];
  if (item.source_kind !== "hook" && item.content) parts.push(item.content);
  if (item.resolution_hint) parts.push(`回收：${item.resolution_hint}`);
  const label = item.source_kind === "hook" ? "钩子" : item.source_kind === "both" ? "钩子/伏笔" : "伏笔";
  return `${label}：${parts.filter(Boolean).join("；")}`;
}

export function buildChapterPlanFromChapter(chapter: OutlineGenChapter): string {
  const parts = [
    ...sanitizeChapterBeats(chapter.beats ?? []),
    ...sanitizePromiseItems(chapter.promise_items ?? [], chapter.number).map((item) => formatPromisePlanLine(item)),
  ];
  return sanitizeLooseTexts(parts).join("；");
}

function hasMeaningfulChapterContent(chapters: OutlineGenChapter[]): boolean {
  return chapters.some(
    (chapter) => sanitizeChapterBeats(chapter.beats ?? []).length > 0 || sanitizePromiseItems(chapter.promise_items ?? [], chapter.number).length > 0,
  );
}

export function parseOutlineMarkdownChapters(markdown: string): OutlineGenChapter[] {
  const deduped = new Map<number, OutlineGenChapter>();
  for (const block of scanMarkdownChapterBlocks(markdown)) {
    if (!(block.number > 0 && Number.isFinite(block.number))) continue;
    deduped.set(block.number, parseMarkdownChapterBlock(block));
  }
  return Array.from(deduped.values()).sort((left, right) => left.number - right.number);
}

export function deriveOutlineFromStoredContent(
  contentMd: string,
  structure: unknown,
): {
  normalizedContentMd: string;
  chapters: OutlineGenChapter[];
} {
  const storedChapters = extractOutlineChapters(structure).map((chapter) => normalizeChapter(chapter));
  if (storedChapters.length > 0) {
    if (hasMeaningfulChapterContent(storedChapters)) {
      return { normalizedContentMd: contentMd, chapters: storedChapters };
    }
    const markdownChapters = parseOutlineMarkdownChapters(contentMd);
    if (markdownChapters.length > 0 && hasMeaningfulChapterContent(markdownChapters)) {
      return {
        normalizedContentMd: contentMd,
        chapters: markdownChapters,
      };
    }
    return { normalizedContentMd: contentMd, chapters: storedChapters };
  }
  const parsed = parseOutlineGenResultFromText(contentMd);
  if (parsed && parsed.chapters.length > 0) {
    return {
      normalizedContentMd: parsed.outline_md || contentMd,
      chapters: parsed.chapters.map((chapter) => normalizeChapter(chapter)),
    };
  }
  const markdownChapters = parseOutlineMarkdownChapters(contentMd);
  if (markdownChapters.length > 0) {
    return {
      normalizedContentMd: contentMd,
      chapters: markdownChapters,
    };
  }
  return { normalizedContentMd: contentMd, chapters: [] };
}
