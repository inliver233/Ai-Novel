import { describe, expect, it } from "vitest";

import {
  buildChapterPlanFromChapter,
  deriveOutlineFromStoredContent,
  parseOutlineGenResultFromText,
  parseOutlineMarkdownChapters,
} from "./outlineParsing";

const RAW_PAYLOAD = JSON.stringify(
  {
    outline_md: "# 整体梗概\n- 手工粘贴的大纲应该被识别",
    chapters: [
      { number: 1, title: "开篇", beats: ["主角醒来", "系统出现"] },
      { number: 2, title: "立足", beats: ["开始营业"] },
    ],
  },
  null,
  2,
);

describe("outlineParsing", () => {
  it("parses outline payload pasted as raw text", () => {
    const parsed = parseOutlineGenResultFromText(RAW_PAYLOAD);

    expect(parsed?.outline_md).toContain("整体梗概");
    expect(parsed?.chapters).toHaveLength(2);
    expect(parsed?.chapters[1]?.number).toBe(2);
  });

  it("derives normalized outline content and chapters from legacy stored json text", () => {
    const derived = deriveOutlineFromStoredContent(RAW_PAYLOAD, null);

    expect(derived.normalizedContentMd).toContain("整体梗概");
    expect(derived.normalizedContentMd.trim().startsWith("{")).toBe(false);
    expect(derived.chapters).toHaveLength(2);
  });

  it("prefers stored structure when it already exists", () => {
    const derived = deriveOutlineFromStoredContent("plain markdown", {
      chapters: [{ number: 9, title: "已存结构", beats: ["x"] }],
    });

    expect(derived.normalizedContentMd).toBe("plain markdown");
    expect(derived.chapters).toHaveLength(1);
    expect(derived.chapters[0]?.number).toBe(9);
  });

  it("parses chapters from plain markdown headings", () => {
    const markdown = `
# 卷一
## 第1章 初入江湖
- 主角离开故乡
- 误入纷争

## 第2章 夜雨惊变
- 结识关键同伴
`;
    const chapters = parseOutlineMarkdownChapters(markdown);
    expect(chapters).toHaveLength(2);
    expect(chapters[0]?.number).toBe(1);
    expect(chapters[0]?.title).toContain("初入江湖");
    expect(chapters[0]?.beats).toContain("主角离开故乡");
    expect(chapters[0]?.promise_items).toHaveLength(0);
  });

  it("dedupes duplicated chapter numbers by keeping the later one", () => {
    const markdown = `
## 第331章 旧版本
- 旧内容

## 第332章 正常章节
- 内容二

## 第331章 新版本
- 新内容
`;
    const chapters = parseOutlineMarkdownChapters(markdown);
    expect(chapters).toHaveLength(2);
    expect(chapters[0]?.number).toBe(331);
    expect(chapters[0]?.title).toContain("新版本");
    expect(chapters[0]?.beats).toContain("新内容");
  });

  it("does not treat chapter reference lines as real chapter headings", () => {
    const markdown = `
## 第18章：药人
- 主线推进

第18章药人线启动回收
第26章旧伤小回收
`;
    const chapters = parseOutlineMarkdownChapters(markdown);
    expect(chapters).toHaveLength(1);
    expect(chapters[0]?.number).toBe(18);
  });

  it("extracts plan beats from 核心区块 and skips placeholder/meta labels", () => {
    const markdown = `
## 第1章：边关收尸人
主要内容
开篇直接落在边关寒夜。
陆照野替边军收尸，动作熟、嘴贫、胆大。

爽点
男主底层生存技能强

钩子
老收尸人提到：别碰红衣尸。

伏笔
埋下章：1
回收：第4章
`;
    const chapters = parseOutlineMarkdownChapters(markdown);
    expect(chapters).toHaveLength(1);
    expect(chapters[0]?.beats).toContain("开篇直接落在边关寒夜。");
    expect(chapters[0]?.beats).toContain("陆照野替边军收尸，动作熟、嘴贫、胆大。");
    expect(chapters[0]?.beats).not.toContain("主要内容");
    expect(chapters[0]?.beats).not.toContain("男主底层生存技能强");
    expect(chapters[0]?.promise_items).toHaveLength(2);
    expect(chapters[0]?.promise_items[0]?.source_kind).toBe("hook");
    expect(chapters[0]?.promise_items[1]?.source_kind).toBe("foreshadow");
    expect(chapters[0]?.promise_items[1]?.resolution_hint).toContain("第4章");
  });

  it("falls back to markdown parsing when stored structure only has placeholders", () => {
    const markdown = `
## 第1章：边关收尸人
主要内容
开篇直接落在边关寒夜。
`;
    const derived = deriveOutlineFromStoredContent(markdown, {
      chapters: [{ number: 1, title: "边关收尸人", beats: ["主要内容"] }],
    });
    expect(derived.chapters).toHaveLength(1);
    expect(derived.chapters[0]?.beats).toContain("开篇直接落在边关寒夜。");
    expect(derived.chapters[0]?.beats).not.toContain("主要内容");
  });

  it("parses realistic outline chapters with hooks and foreshadows", () => {
    const markdown = `
第1章：边关收尸人
主要内容

开篇直接落在边关寒夜。
陆照野替边军收尸，动作熟、嘴贫、胆大，几句对话就把他的生存状态和人设立住。

爽点
男主底层生存技能强
见死人不怕，显得野
钩子

老收尸人提到一句：
“这几天关外不太平，别碰红衣尸。”

伏笔
红衣尸规矩
埋下章：1
回收：第4章无面嫁尸登场；第5章尸宴爆发；后期照骨天宫祭典再大回收
第2章：阿篱留灯
主要内容

补强陆照野和阿篱的关系。

钩子
边军忽然敲门，要药铺连夜送药去北营。

伏笔
阿篱“给你留灯”
埋下章：2
回收：第15章暂时呼应；第六阶段“灯下断线”大回收
第3章：三百新尸
主要内容

败军回城，三百具尸体拉进关。

钩子
大红布角下像露出一只苍白女人手。
`;
    const chapters = parseOutlineMarkdownChapters(markdown);
    expect(chapters).toHaveLength(3);
    expect(chapters[0]?.number).toBe(1);
    expect(chapters[0]?.promise_items).toHaveLength(2);
    expect(chapters[0]?.promise_items[0]?.content).toContain("别碰红衣尸");
    expect(chapters[0]?.promise_items[1]?.title).toBe("红衣尸规矩");
    expect(chapters[0]?.promise_items[1]?.planned_chapter_numbers).toEqual([4, 5]);
    expect(chapters[1]?.promise_items[1]?.planned_chapter_numbers).toEqual([15]);
    expect(chapters[2]?.promise_items[0]?.source_kind).toBe("hook");
  });

  it("accepts inline section labels without forcing separate lines", () => {
    const chapters = parseOutlineMarkdownChapters(`
第1章：边关收尸人
主要内容：开篇直接落在边关寒夜。
爽点：男主底层生存技能强
钩子：老收尸人提到一句：别碰红衣尸。
伏笔：红衣尸规矩
回收：第4章无面嫁尸登场
`);
    expect(chapters).toHaveLength(1);
    expect(chapters[0]?.beats).toContain("开篇直接落在边关寒夜。");
    expect(chapters[0]?.highlights).toContain("男主底层生存技能强");
    expect(chapters[0]?.promise_items).toHaveLength(2);
    expect(chapters[0]?.promise_items[0]?.content).toContain("别碰红衣尸");
    expect(chapters[0]?.promise_items[1]?.resolution_hint).toContain("第4章无面嫁尸登场");
  });

  it("merges duplicated hook and foreshadow entries into one promise item", () => {
    const chapters = parseOutlineMarkdownChapters(`
第1章：边关收尸人
主要内容
开篇直接落在边关寒夜。

钩子
别碰红衣尸

伏笔
别碰红衣尸
`);
    expect(chapters).toHaveLength(1);
    expect(chapters[0]?.promise_items).toHaveLength(1);
    expect(chapters[0]?.promise_items[0]?.source_kind).toBe("both");
  });

  it("builds chapter plan from beats and promise items", () => {
    const chapter = parseOutlineMarkdownChapters(`
第1章：边关收尸人
主要内容
开篇直接落在边关寒夜。

钩子
老收尸人提到一句：
“这几天关外不太平，别碰红衣尸。”

伏笔
红衣尸规矩
回收：第4章无面嫁尸登场
`)[0];
    const plan = buildChapterPlanFromChapter(chapter);
    expect(plan).toContain("开篇直接落在边关寒夜。");
    expect(plan).toContain("钩子：老收尸人提到一句： “这几天关外不太平，别碰红衣尸。”");
    expect(plan).toContain("伏笔：红衣尸规矩");
    expect(plan).toContain("回收：第4章无面嫁尸登场");
  });
});
