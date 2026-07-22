"""Companion World 真人级主动触达的纯领域契约与决策。

本模块只描述 scope、发声人与路由选择规则；数据库查询和投递由 platform adapter 实现。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


HUMAN_PROACTIVE_CATEGORIES = frozenset(
    {"new_user_reactivation", "content_invitation", "companion_followup"}
)


@dataclass(frozen=True)
class HumanProactiveSpeaker:
    """App 通知最终可展示的 active resident。"""

    resident_id: str
    runtime_account_id: str
    conversation_id: str
    last_inbound_at: Optional[str] = None
    last_app_activity_at: Optional[str] = None
    joined_at: Optional[str] = None


@dataclass(frozen=True)
class HumanProactiveScope:
    """一个 runtime account 解析出的真人级聚合范围。"""

    platform_user_id: str
    universe_id: str
    runtime_account_ids: Tuple[str, ...]
    requested_resident_id: str
    owner_created_at: str
    legacy_primary_account_id: Optional[str]
    legacy_weixin_route_available: bool
    app_speaker: Optional[HumanProactiveSpeaker]


@dataclass(frozen=True)
class HumanProactiveDecision:
    """真人级触达的唯一通道决策；``blocked`` 表示 fail-closed。"""

    mode: str  # weixin | app_inbox | blocked
    reason: Optional[str] = None


def is_human_proactive_category(category: str) -> bool:
    """返回 product category 是否进入真人级预算与 App 24h 桶。"""

    return str(category or "").strip() in HUMAN_PROACTIVE_CATEGORIES


def decide_human_proactive_delivery(
    *,
    legacy_weixin_route_available: bool,
    app_inbox_enabled: bool,
    app_only_human_enabled: bool,
    has_app_speaker: bool,
) -> HumanProactiveDecision:
    """按“真实微信优先、App-only 双 flag”冻结规则选择通道。"""

    if legacy_weixin_route_available:
        return HumanProactiveDecision(mode="weixin")
    if app_inbox_enabled and app_only_human_enabled and has_app_speaker:
        return HumanProactiveDecision(mode="app_inbox")
    return HumanProactiveDecision(
        mode="blocked", reason="companion_world_human_level_proactive_blocked"
    )


def allows_speaker_reselection(*, speaker_bound: bool) -> bool:
    """通用内容可在投递事务重选发声人；强绑定内容必须保留原 speaker。"""

    return not bool(speaker_bound)


__all__ = [
    "HUMAN_PROACTIVE_CATEGORIES",
    "HumanProactiveDecision",
    "HumanProactiveScope",
    "HumanProactiveSpeaker",
    "allows_speaker_reselection",
    "decide_human_proactive_delivery",
    "is_human_proactive_category",
]
