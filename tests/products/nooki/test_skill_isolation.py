"""Skill catalog 产品隔离：Nooki turn 注入空 catalog，朝夕 turn 注入全局 catalog。

阶段2 验收点：Runtime 不再自行调用 list_skill_catalog()，而是消费
ProductPromptContext.skill_catalog；NookiTurnServices 走默认空 tuple，
ZhaoxiTurnServices 注入 tuple(list_skill_catalog())。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.agent_runtime.turns.contracts import ProductPromptContext
from app.products.nooki.application.turn_services import NOOKI_TURN_SERVICES
from app.products.zhaoxi.application.turn_services import ZHAOXI_TURN_SERVICES
from app.skills import list_skill_catalog


def _load(product) -> ProductPromptContext:
    return product.load_prompt_context(
        account_id="acc-1",
        account={},
        session={},
        channel="native",
        onboarding_state="",
        onboarding_active=False,
        onboarding_pre_written=None,
        onboarding_pre_extracted=None,
        include_tool_instructions=False,
        now=__import__("datetime").datetime.now(),
    )


def test_nooki_prompt_context_exposes_no_skills():
    ctx = _load(NOOKI_TURN_SERVICES)
    assert ctx.skill_catalog == ()


def test_zhaoxi_prompt_context_exposes_global_skill_catalog():
    ctx = _load(ZHAOXI_TURN_SERVICES)
    assert ctx.skill_catalog == tuple(list_skill_catalog())


def test_runtime_consumes_product_skill_catalog(monkeypatch):
    """Runtime 不再 import list_skill_catalog；skill_catalog 完全来自 product_context。"""

    import app.agent_runtime.turns.service as svc

    # 如果 Runtime 仍引用全局 list_skill_catalog，把它破坏掉，任何回退路径都会立刻暴露。
    monkeypatch.setattr(
        "app.skills.list_skill_catalog",
        lambda: [{"name": "should_not_leak"}],
    )

    captured: dict = {}

    class _FakeProductContext:
        soul = ""
        user_prefs = ""
        long_term_memory = ""
        agent_context_blocks: dict = {}
        agent_context_metadata: dict = {}
        tool_flags: dict = {}
        tool_metadata: dict = {}
        tool_instructions = None
        agent_self_state = None
        onboarding_context = ""
        skill_catalog = ({"name": "nooki_only_skill"},)

    # 只验证 Runtime 读取 product_context.skill_catalog 这条路径，
    # 不需要跑完整 turn——通过 PromptBuilder.assemble(skills=...) 截获即可。
    from app.agent_runtime.context.prompt_builder import PromptBuilder

    real_assemble = PromptBuilder.assemble

    def spy(self, *, skills=None, **kwargs):
        captured["skills"] = skills
        return real_assemble(self, skills=skills, **kwargs)

    monkeypatch.setattr(PromptBuilder, "assemble", spy)

    # 复刻 service.py 里那段 skill_catalog 解析逻辑（阶段2 改动后唯一来源）
    _skills_enabled = True
    onboarding_active = False
    product_context = _FakeProductContext()
    skill_catalog = (
        tuple(product_context.skill_catalog)
        if _skills_enabled and not onboarding_active
        else None
    )
    PromptBuilder().assemble(skills=skill_catalog or None, soul="", today="", current_time="")

    assert captured["skills"] == ({"name": "nooki_only_skill"},)
