"""M3 App 拉取式通知纯领域服务。"""
from __future__ import annotations

from typing import Optional, Tuple

from app.products.zhaoxi.domain.companion_world.contracts import (
    AppNotificationRecord,
    AppNotificationRepository,
    CompanionWorldError,
)


class CompanionWorldNotificationService:
    """编排通知读取/已读语义；SQL、时间计算和 HTTP 留给 adapter。"""

    def __init__(self, repository: AppNotificationRepository) -> None:
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
    ) -> Tuple[AppNotificationRecord, ...]:
        """列出有效 visible 通知；读取本身不改变 read 状态。"""
        if status not in {"all", "unread"} or limit < 1 or limit > 51:
            raise CompanionWorldError("invalid_request")
        return tuple(
            self._repository.list_visible_notifications(
                platform_user_id=platform_user_id,
                now=now,
                unread_only=status == "unread",
                cursor_delivered_at=cursor_delivered_at,
                cursor_notification_id=cursor_notification_id,
                limit=limit,
            )
        )

    def count_unread(self, platform_user_id: str, *, now: str) -> int:
        """读取真人当前有效 unread 数。"""
        return self._repository.count_unread(
            platform_user_id=platform_user_id, now=now
        )

    def mark_read(
        self,
        platform_user_id: str,
        *,
        notification_id: str,
        now: str,
        read_expires_at: str,
    ) -> AppNotificationRecord:
        """幂等标记单条已读；越权/过期/隐藏统一 not found。"""
        row = self._repository.mark_read(
            platform_user_id=platform_user_id,
            notification_id=notification_id,
            now=now,
            read_expires_at=read_expires_at,
        )
        if row is None:
            raise CompanionWorldError("notification_not_found")
        return row

    def mark_all_read(
        self, platform_user_id: str, *, now: str, read_expires_at: str
    ) -> Tuple[int, str]:
        """在线性化 owner 锁内标记当前全部有效 unread。"""
        return self._repository.mark_all_read(
            platform_user_id=platform_user_id,
            now=now,
            read_expires_at=read_expires_at,
        )
