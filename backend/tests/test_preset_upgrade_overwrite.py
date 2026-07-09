"""B 类 known_issue 测试：preset 版本升级覆盖用户自定义 template。

M25 — app/services/prompt_preset_defaults.py 的 _ensure_default_preset_from_resource
在版本升级分支（line 79-106）无条件执行 ``existing.template = str(res_block.template or "")``，
把用户已自定义的 block template 覆盖为默认值；且升级出错被 get_active_preset_for_task
里的 bare ``except Exception: pass`` 吞掉（line 271-272 / 286-287），用户无法察觉。

正确行为：若用户已自定义某 block（existing.template 与默认值不同），版本升级时应
保留用户自定义，不应覆盖。当前实现有 bug，故本测试必须 FAILED(red)。
"""
# M25

import json

import pytest
from sqlalchemy import select

from app.db.utils import new_id
from app.models.prompt_block import PromptBlock
from app.models.prompt_preset import PromptPreset
from app.services.prompt_preset_defaults import ensure_default_plan_preset
from app.services.prompt_preset_resources import load_preset_resource
from tests.support import create_tables, make_session_factory, make_sqlite_engine

RESOURCE_KEY = "plan_chapter_v1"


@pytest.mark.known_issue
def test_upgrade_preserves_user_customized_block_template():
    """版本升级时应保留用户已自定义的 block template，不应被默认值覆盖。

    复现路径：
      1. DB 插入一个旧版本（version=0）的 PromptPreset，绑定 resource_key=plan_chapter_v1；
      2. 为其插入一个 PromptBlock，template 设为用户自定义值（与资源默认值不同）；
      3. 调用公开入口 ensure_default_plan_preset 触发版本升级（0 < 1）；
      4. 升级后回读该 block.template。

    正确行为：用户自定义的 template 仍保留（== 自定义值）。
    当前 bug：被默认值覆盖 -> 断言失败(red)。
    """
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine)

    resource = load_preset_resource(RESOURCE_KEY)
    res_block = resource.blocks[0]  # sys.plan_chapter.role
    default_template = str(res_block.template or "")
    user_custom_template = "USER_CUSTOMIZED_TEMPLATE__SHOULD_SURVIVE_UPGRADE"
    assert user_custom_template != default_template

    project_id = "proj-m25-upgrade"

    with factory() as db:
        # 1. 旧版本 preset，绑定默认资源（version=0 < resource.version=1 触发升级）
        preset = PromptPreset(
            id=new_id(),
            project_id=project_id,
            name=resource.name,
            resource_key=RESOURCE_KEY,
            category=resource.category,
            scope=resource.scope,
            version=0,
            active_for_json=json.dumps(resource.activation_tasks, ensure_ascii=False),
        )
        db.add(preset)
        db.flush()

        # 2. 用户自定义的 block（template 与默认值不同）
        block = PromptBlock(
            id=new_id(),
            preset_id=preset.id,
            identifier=res_block.identifier,
            name=res_block.name,
            role=res_block.role,
            enabled=bool(res_block.enabled),
            template=user_custom_template,
            marker_key=res_block.marker_key,
            injection_position=res_block.injection_position,
            injection_depth=res_block.injection_depth,
            injection_order=int(res_block.injection_order),
            triggers_json=json.dumps(list(res_block.triggers or []), ensure_ascii=False),
            forbid_overrides=bool(res_block.forbid_overrides),
            budget_json=json.dumps(res_block.budget, ensure_ascii=False) if res_block.budget else None,
            cache_json=None,
        )
        db.add(block)
        db.commit()

        # 3. 触发版本升级（公开入口）
        ensure_default_plan_preset(db, project_id=project_id)

        # 4. 回读该 block
        upgraded = (
            db.execute(
                select(PromptBlock).where(
                    PromptBlock.preset_id == preset.id,
                    PromptBlock.identifier == res_block.identifier,
                )
            )
            .scalars()
            .first()
        )
        assert upgraded is not None

        # 正确行为：用户自定义 template 应保留，不应被默认值覆盖
        assert upgraded.template == user_custom_template, (
            "版本升级不应覆盖用户已自定义的 block template；"
            f"期望={user_custom_template!r}，实际={upgraded.template!r}"
        )

    engine.dispose()
