"""测试共享工厂：消除各测试文件里 copy-paste 的账号/路由 setup。

P3 冗余审计 finding #1：`_create_account` 曾在 ~30 个测试文件、`_create_route` 在 12 个
文件各自定义且几乎逐字节相同。此处收敛为单一来源，新增测试直接复用，避免继续扩散。

不覆盖以下两个语义不同的本地 helper（保留各自定义）：
- test_billing_charges：经 phone→platform user→default account 建号，返回 account_id（str）。
- test_node_agent：sender_id/chat_id 取 "s"/"c"（与此处 "sender"/"chat" 不同），保留其存储身份值。
"""
from typing import Optional


def create_account(
    account_id: str,
    *,
    business_day: Optional[str] = None,
    node_id: Optional[str] = None,
    is_debug: bool = False,
) -> int:
    """建一个微信账号的 active session，返回 session id（int）。

    历史上多数调用方声明返回 None 并忽略返回值——统一返回 int 对它们行为不变。
    ``business_day`` 非空时透传给 get_or_create_session；``node_id``/``is_debug``
    非默认时触发对应的账号级设置（分别对应 multi_node、user_meta 两处旧变体）。
    """
    from app.db import get_or_create_session

    kwargs = dict(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"session-{account_id}",
    )
    if business_day is not None:
        kwargs["business_day"] = business_day
    state = get_or_create_session(**kwargs)
    if node_id is not None:
        from app.db import set_account_assigned_node

        set_account_assigned_node(account_id=account_id, node_id=node_id)
    if is_debug:
        from app.db import set_account_debug_flag

        set_account_debug_flag(account_id=account_id, is_debug=True)
    return int(state["session"]["id"])


def create_route(account_id: str, *, session_key: Optional[str] = None) -> None:
    """登记一条最近入站的 channel_binding，使账号被 touch_state 判为 reachable。

    ``session_key`` 缺省回落 ``f"session-{account_id}"``（与旧各文件一致）。
    """
    from app.db import upsert_channel_binding

    upsert_channel_binding(
        account_id=account_id,
        channel="openclaw-weixin",
        session_key=session_key or f"session-{account_id}",
        channel_account_id="bot-1",
        sender_id="sender",
        chat_id="user@im.wechat",
        raw_identity={"source": "test"},
    )


def make_resident_account(platform_user_id: str, display_name: str) -> str:
    """为一个真人建「第 2..N 个账号」= 一个居民 runtime account（form-B），返回 account_id。

    账号模型决策 B（docs/tech_design/companion_world_account_model_reconciliation.md）：main #42
    收敛后「一手机号 × 一 App = 一个用户账号」，用 create_ai4all_account_for_user 建同一真人的第二号
    会撞软检查 existing_count>=1。真人的多个 agent = 世界里的居民（form-B runtime account），经
    「世界归属」解析共享真人钱包/配额、**不发 owner_binding**（owner_binding 是微信接入独有的产物）。
    「一人多号共享钱包/配额」类测试用此建第 2..N 号（不再用用户注册入口伪造）。

    幂等 get-or-create home universe + 一个官方模板 + 居民内部建号原语。
    """
    from app.db import (
        create_character_template,
        create_resident_runtime_account,
        get_or_create_home_universe,
    )

    universe = get_or_create_home_universe(platform_user_id=platform_user_id)
    template = create_character_template(source_type="official", name=f"tmpl-{display_name}")
    return create_resident_runtime_account(
        universe_id=universe["id"],
        character_template_id=template["id"],
        display_name=display_name,
    )["account"]["id"]
