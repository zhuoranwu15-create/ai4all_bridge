"""异步居民许愿的纯领域状态与客户端投影。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


PUBLIC_PENDING_STATUSES = frozenset({"pending"})
PUBLIC_WISH_STATUSES = frozenset({"pending", "delivered", "withdrawn", "unfulfilled"})


@dataclass(frozen=True)
class ResidentWishRecord:
    """一笔 owner/world 隔离的许愿事实。"""

    id: str
    owner_platform_user_id: str
    universe_id: str
    client_request_id: str
    request_fingerprint: str
    status: str
    submitted_at: str
    deliver_not_before: str
    deliver_by: str
    letter_id: Optional[str] = None
    closed_at: Optional[str] = None
    terminal_reason: Optional[str] = None


@dataclass(frozen=True)
class ResidentWishProjection:
    """允许下发给 App 的稳定字段；不包含原始愿望或任务内部进度。"""

    wish_id: str
    status: str
    submitted_at: str
    expected_delivery_from: str
    expected_delivery_to: str
    letter_id: Optional[str]
    is_open: bool
    can_withdraw: bool
    terminal_reason: Optional[str]


def project_resident_wish(record: ResidentWishRecord) -> ResidentWishProjection:
    """把持久状态投影成冻结的四态客户端契约。"""
    status = record.status if record.status in PUBLIC_WISH_STATUSES else "pending"
    is_open = record.closed_at is None
    return ResidentWishProjection(
        wish_id=record.id,
        status=status,
        submitted_at=record.submitted_at,
        expected_delivery_from=record.deliver_not_before,
        expected_delivery_to=record.deliver_by,
        letter_id=record.letter_id,
        is_open=is_open,
        can_withdraw=is_open and status == "pending",
        terminal_reason=record.terminal_reason,
    )


__all__ = [
    "PUBLIC_PENDING_STATUSES",
    "PUBLIC_WISH_STATUSES",
    "ResidentWishProjection",
    "ResidentWishRecord",
    "project_resident_wish",
]
