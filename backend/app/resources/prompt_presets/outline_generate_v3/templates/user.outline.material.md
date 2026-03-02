<PROJECT>
名称：{{project_name}}
题材：{{genre}}
一句话梗概：{{logline}}
</PROJECT>

{% if world_setting %}<WORLD_SETTING>
{{world_setting}}
</WORLD_SETTING>

{% endif %}{% if characters %}<CHARACTERS>
{{characters}}
</CHARACTERS>

{% endif %}{% if style_guide %}<STYLE_GUIDE>
{{style_guide}}
</STYLE_GUIDE>

{% endif %}{% if constraints %}<CONSTRAINTS>
{{constraints}}
</CONSTRAINTS>

{% endif %}<REQUIREMENTS_JSON>
{{requirements}}
</REQUIREMENTS_JSON>

{% if target_chapter_count %}<CHAPTER_TARGET>
目标章节数：{{target_chapter_count}}（请严格保证 chapters 数组条目数与之相同）
</CHAPTER_TARGET>
{% endif %}
