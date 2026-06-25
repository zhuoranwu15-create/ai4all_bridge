"""P1-2：声明式 block 流水线 / 自产元数据 / token 预算机制（默认关闭）测试。

现有 test_prompt_builder.py 已锁定"输出逐字不变"；这里只覆盖新增能力。
"""
from app.prompt_builder import (
    BuildResult,
    ContextBlock,
    PromptBuilder,
    _SECTION_VOLATILE,
)


def _pb():
    return PromptBuilder()


def test_assemble_returns_buildresult_and_build_matches_prompt():
    pb = _pb()
    kwargs = dict(display_name="小助手", soul="你是温暖的助手。", today="2026-06-16", model_name="m1")
    result = pb.assemble(**kwargs)
    assert isinstance(result, BuildResult)
    # build() 必须等价于 assemble().prompt（向后兼容契约）
    assert pb.build(**kwargs) == result.prompt
    # 每个已组装 block 都有元数据
    assert result.blocks and all(b.chars > 0 for b in result.blocks)


def test_buildresult_daily_notes_included_and_chars():
    pb = _pb()
    # 不传 daily_notes → 未组装 → included False / chars 0（替代历史写死的僵尸字段）
    none_res = pb.assemble(today="2026-06-16")
    assert none_res.included("daily_notes") is False
    assert none_res.final_chars("daily_notes") == 0
    # 传 daily_notes → included True / chars 为带标记文本长度
    got_res = pb.assemble(daily_notes="今天买菜", today="2026-06-16")
    assert got_res.included("daily_notes") is True
    assert got_res.final_chars("daily_notes") == len("【今日备注】\n今天买菜")


def test_extra_blocks_injected_and_noop_when_absent():
    pb = _pb()
    base = pb.build(today="2026-06-16")
    extra = ContextBlock(name="retrieved_notes", text="【检索注入】这是动态来源", section=_SECTION_VOLATILE)
    with_extra = pb.assemble(today="2026-06-16", extra_blocks=[extra])
    # 注入内容出现，且位于内置 block 之后（追加在末尾）
    assert "【检索注入】这是动态来源" in with_extra.prompt
    assert with_extra.prompt.startswith(base)
    assert with_extra.included("retrieved_notes") is True
    # 不传 extra_blocks 时与原输出一致（无影响）
    assert pb.assemble(today="2026-06-16").prompt == base


def test_token_budget_none_is_noop():
    pb = _pb()
    kwargs = dict(
        display_name="X", soul="Y", carryover_summary="上次说到签证",
        daily_notes="买菜", today="2026-06-16", model_name="m",
    )
    assert pb.assemble(**kwargs, token_budget=None).prompt == pb.build(**kwargs)


def test_token_budget_drops_low_priority_volatile_keeps_stable():
    pb = _pb()
    # 给若干 volatile block + 一个很小的预算，迫使裁剪。
    res = pb.assemble(
        agent_context={"MEMORY": "记忆内容" * 50},  # project_context (volatile, prio 80)
        carryover_summary="延续摘要" * 50,            # carryover (volatile, prio 20)
        daily_notes="今日备注" * 50,                  # daily_notes (volatile, prio 10 → 最先丢)
        today="2026-06-16",
        token_budget=120,
    )
    # stable 核心必须保留
    assert "【微信回复呈现】" in res.prompt   # output_directives (stable)
    assert res.included("output_directives") is True
    assert res.included("factual_discipline") is True
    # 最低优先级的 daily_notes 应最先被丢弃
    assert res.included("daily_notes") is False
    assert "【今日备注】" not in res.prompt


def test_token_budget_keeps_all_when_within():
    pb = _pb()
    # 极大预算 → 不裁剪，等价于无预算。
    kwargs = dict(daily_notes="买菜", carryover_summary="签证", today="2026-06-16")
    assert pb.assemble(**kwargs, token_budget=10_000_000).prompt == pb.build(**kwargs)
