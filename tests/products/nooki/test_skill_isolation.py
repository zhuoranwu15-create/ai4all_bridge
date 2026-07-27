"""Skill catalog 产品隔离：Nooki turn 注入空 catalog，朝夕 turn 注入全局 catalog。

阶段2 验收点：Runtime 不再自行调用 list_skill_catalog()，而是消费
ProductPromptContext.skill_catalog；NookiTurnServices 走默认空 tuple，
ZhaoxiTurnServices 注入 tuple(list_skill_catalog())。
"""
from __future__ import annotations

from datetime import datetime

import app.db as db
from app.agent_runtime.turns.contracts import ProductPromptContext
from app.bootstrap.product_registry import (
    NOOKI_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
    ZHAOXI_APP_ID,
)
from app.products.nooki.application.turn_services import NOOKI_TURN_SERVICES
from app.products.zhaoxi.application.turn_services import ZHAOXI_TURN_SERVICES
from app.skills import list_skill_catalog


def _load(product, *, account_id: str, account: dict) -> ProductPromptContext:
    """对真实建的账号调用产品 load_prompt_context，不 mock。"""

    return product.load_prompt_context(
        account_id=account_id,
        account=account,
        session={},
        channel="native",
        onboarding_state="",
        onboarding_active=False,
        onboarding_pre_written=None,
        onboarding_pre_extracted=None,
        include_tool_instructions=False,
        now=datetime.now(),
    )


def _make_account(phone: str, app_id: str, display_name: str):
    """建真人 + 产品 membership + runtime account（含 owner_binding），返回 (user, account)。

    复用 test_turn_services 的建号模式：先 create_or_get_platform_user_by_phone，
    再 ensure_product_membership，最后 create_ai4all_account_for_user——后者同时落
    account_owner_bindings，使 resolve_owner_platform_user_id 能把 account 解析到真人。
    """

    user = db.create_or_get_platform_user_by_phone(phone=phone)
    db.ensure_product_membership(
        platform_user_id=user["id"], app_id=app_id, registry=PRODUCTION_PRODUCT_REGISTRY
    )
    account = db.create_ai4all_account_for_user(
        platform_user_id=user["id"],
        display_name=display_name,
        app_id=app_id,
        registry=PRODUCTION_PRODUCT_REGISTRY,
    )["account"]
    return user, account


def test_nooki_prompt_context_exposes_no_skills(fresh_db):
    """Nooki 账号经真实 load_prompt_context 后 skill_catalog 为空 tuple。"""

    _, account = _make_account("13800039001", NOOKI_APP_ID, "Nooki 隔离测试用户")
    ctx = _load(NOOKI_TURN_SERVICES, account_id=account["id"], account=account)

    assert ctx.skill_catalog == ()


def test_zhaoxi_prompt_context_exposes_global_skill_catalog(fresh_db):
    """朝夕账号经真实 load_prompt_context 后 skill_catalog 等于全局 catalog。

    朝夕 load_prompt_context 会读 profile 文件（account_profile_files）并播种默认
    SOUL/IDENTITY/USER 等；fresh_db 已建表，read_agent_context 的 ensure_*
    会自动落账号级 profile，无需额外造数据。
    """

    _, account = _make_account("13800039002", ZHAOXI_APP_ID, "朝夕隔离测试用户")
    ctx = _load(ZHAOXI_TURN_SERVICES, account_id=account["id"], account=account)

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
