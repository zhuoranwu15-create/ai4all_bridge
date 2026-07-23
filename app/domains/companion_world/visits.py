"""Companion World M5 invite/visit 纯领域 DTO 与状态机。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Protocol, Sequence

INVITE_STATUSES = frozenset({"active", "redeemed", "revoked", "expired"})
VISIT_OPEN_STATUSES = frozenset({"pending", "active"})
VISIT_TERMINAL_STATUSES = frozenset(
    {"expired", "rejected", "cancelled", "left", "revoked", "blocked"}
)

_VISIT_TRANSITIONS = {
    "pending": frozenset({"active", "expired", "rejected", "cancelled", "blocked"}),
    "active": frozenset({"expired", "left", "revoked", "blocked"}),
}


@dataclass(frozen=True)
class VisitPolicy:
    """M5 首版不可漂移的容量与绝对期限。"""

    owner_slot_limit: int = 3
    visitor_open_limit: int = 3
    invite_ttl_hours: int = 24
    pending_ttl_days: int = 7
    active_ttl_days: int = 30


@dataclass(frozen=True)
class UniverseInviteRecord:
    """不含邀请码明文的持久化 invite 视图。"""

    id: str
    universe_id: str
    owner_platform_user_id: str
    code_hash: str
    code_prefix: str
    status: str
    expires_at: str
    created_at: str
    redeemed_by_platform_user_id: Optional[str] = None
    redeemed_visit_id: Optional[str] = None


@dataclass(frozen=True)
class UniverseVisitRecord:
    """owner/visitor/universe 三锚固定的 visit。"""

    id: str
    invite_id: str
    universe_id: str
    owner_platform_user_id: str
    visitor_platform_user_id: str
    status: str
    pending_expires_at: str
    created_at: str
    accepted_at: Optional[str] = None
    expires_at: Optional[str] = None
    terminal_at: Optional[str] = None
    terminal_reason: Optional[str] = None


def visit_transition_allowed(current_status: str, target_status: str) -> bool:
    """返回 visit 状态迁移是否符合 M5 单向状态机。"""
    return target_status in _VISIT_TRANSITIONS.get(current_status, frozenset())


def invite_expires_at(*, created_at: datetime, policy: VisitPolicy) -> datetime:
    """返回 invite 从创建时刻计算的绝对过期时间。"""
    return created_at + timedelta(hours=policy.invite_ttl_hours)


def pending_expires_at(*, redeemed_at: datetime, policy: VisitPolicy) -> datetime:
    """返回 pending 从兑换时刻计算的绝对审批截止时间。"""
    return redeemed_at + timedelta(days=policy.pending_ttl_days)


def active_expires_at(*, accepted_at: datetime, policy: VisitPolicy) -> datetime:
    """返回 active visit 从接受时刻计算的绝对过期时间。"""
    return accepted_at + timedelta(days=policy.active_ttl_days)


def is_due(*, now: datetime, expires_at: datetime) -> bool:
    """统一执行 ``now >= expires_at`` 的精确到期语义。"""
    return now >= expires_at


class VisitRepository(Protocol):
    """M5 visit 持久化端口；实现必须强制 owner/visitor 隔离。"""

    def get_invite_for_owner(
        self, *, invite_id: str, platform_user_id: str
    ) -> Optional[UniverseInviteRecord]: ...

    def get_visit_for_participant(
        self, *, visit_id: str, platform_user_id: str
    ) -> Optional[UniverseVisitRecord]: ...

    def list_visits_for_participant(
        self, *, platform_user_id: str, statuses: Sequence[str], limit: int
    ) -> Sequence[UniverseVisitRecord]: ...


__all__ = [
    "INVITE_STATUSES",
    "VISIT_OPEN_STATUSES",
    "VISIT_TERMINAL_STATUSES",
    "UniverseInviteRecord",
    "UniverseVisitRecord",
    "VisitPolicy",
    "VisitRepository",
    "active_expires_at",
    "invite_expires_at",
    "is_due",
    "pending_expires_at",
    "visit_transition_allowed",
]
