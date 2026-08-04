"""鸣蝉 App 通知的纯领域模型与读取/已读编排。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Sequence, Tuple

from app.products.mingchan.domain.companion_world import MingchanWorldError


@dataclass(frozen=True)
class MingchanNotificationRecord:
    """客户端可见通知所需的最小领域投影。"""

    id: str
    platform_user_id: str
    universe_id: str
    scope: str
    category: str
    source_type: str
    delivery_status: str
    resident_id: Optional[str]
    resident_name: Optional[str]
    resident_avatar_ref: Optional[str]
    title: Optional[str]
    body_text: Optional[str]
    target_type: str
    target_id: Optional[str]
    delivered_at: Optional[str]
    read_at: Optional[str]
    expires_at: Optional[str]


class MingchanNotificationRepository(Protocol):
    """鸣蝉通知持久化端口；实现必须固定 ``app_id=mingchan``。"""

    def list_visible(
        self,
        *,
        platform_user_id: str,
        now: str,
        unread_only: bool,
        cursor_delivered_at: Optional[str],
        cursor_notification_id: Optional[str],
        limit: int,
    ) -> Sequence[MingchanNotificationRecord]: ...

    def count_unread(self, *, platform_user_id: str, now: str) -> int: ...

    def mark_read(
        self,
        *,
        platform_user_id: str,
        notification_id: str,
        now: str,
        read_expires_at: str,
    ) -> Optional[MingchanNotificationRecord]: ...

    def mark_all_read(
        self, *, platform_user_id: str, now: str, read_expires_at: str
    ) -> Tuple[int, str]: ...


class MingchanNotificationService:
    """编排鸣蝉通知列表和已读语义，不持有 HTTP 或 SQL 细节。"""

    def __init__(self, repository: MingchanNotificationRepository) -> None:
        self._repository = repository

    def list_notifications(
        self,
        platform_user_id: str,
        *,
        now: str,
        status: str,
        cursor_delivered_at: Optional[str],
        cursor_notification_id: Optional[str],
        limit: int,
    ) -> Tuple[MingchanNotificationRecord, ...]:
        """返回指定真人的有效通知，读取本身不改变状态。"""

        if status not in {"all", "unread"} or limit < 1 or limit > 51:
            raise MingchanWorldError("invalid_request")
        return tuple(
            self._repository.list_visible(
                platform_user_id=platform_user_id,
                now=now,
                unread_only=status == "unread",
                cursor_delivered_at=cursor_delivered_at,
                cursor_notification_id=cursor_notification_id,
                limit=limit,
            )
        )

    def count_unread(self, platform_user_id: str, *, now: str) -> int:
        """返回指定真人的鸣蝉未读通知数。"""

        return self._repository.count_unread(
            platform_user_id=platform_user_id,
            now=now,
        )

    def mark_read(
        self,
        platform_user_id: str,
        *,
        notification_id: str,
        now: str,
        read_expires_at: str,
    ) -> MingchanNotificationRecord:
        """幂等标记单条通知已读；越权与不存在统一返回稳定错误。"""

        item = self._repository.mark_read(
            platform_user_id=platform_user_id,
            notification_id=notification_id,
            now=now,
            read_expires_at=read_expires_at,
        )
        if item is None:
            raise MingchanWorldError("notification_not_found")
        return item

    def mark_all_read(
        self,
        platform_user_id: str,
        *,
        now: str,
        read_expires_at: str,
    ) -> Tuple[int, str]:
        """线性化标记当前全部鸣蝉通知已读。"""

        return self._repository.mark_all_read(
            platform_user_id=platform_user_id,
            now=now,
            read_expires_at=read_expires_at,
        )


__all__ = [
    "MingchanNotificationRecord",
    "MingchanNotificationRepository",
    "MingchanNotificationService",
]
