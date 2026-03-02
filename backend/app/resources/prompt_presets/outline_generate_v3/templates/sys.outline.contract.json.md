【输出格式契约：必须严格遵守】
你必须只输出一个 JSON 对象，标签外禁止任何文字；不要 Markdown，不要代码块。
JSON Schema：
{
  "outline_md": string,
  "chapters": [
    {"number": int, "title": string, "beats": [string]}
  ]
}

约束：
- chapters 的 number 从 1 递增且不重复
{% if chapter_count_rule %}- {{chapter_count_rule}}
{% endif %}{% if chapter_detail_rule %}- {{chapter_detail_rule}}
{% else %}- beats 每章 5~9 条，按发生顺序；每条用短句，明确“发生了什么/造成什么后果”{% endif %}
- 若输出长度受限，必须优先保证章节数量与编号完整；可压缩每章 beats 的字数与条数，但不得减少章节总数
- outline_md 用 Markdown 写“整体梗概/人物主线/悬念与伏笔分布/节奏规划”，不要写成正文
