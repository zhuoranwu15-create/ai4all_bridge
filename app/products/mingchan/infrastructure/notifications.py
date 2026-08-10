"""鸣蝉通知的 PostgreSQL adapter。"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

from app.bootstrap.product_registry import MINGCHAN_APP_ID
from app.products.mingchan.domain.notifications import MingchanNotificationRecord
from app.products.mingchan.infrastructure.persistence import notifications as notification_db


def _notification(row: Dict[str, Any]) -> MingchanNotificationRecord:
    """把产品过滤后的存储行投影为鸣蝉领域对象。"""

    return MingchanNotificationRecord(
        id=str(row["id"]),
        platform_user_id=str(row["platform_user_id"]),
        universe_id=str(row["universe_id"]),
        scope=str(row["scope"]),
        category=str(row["category"]),
        source_type=str(row["source_type"]),
        delivery_status=str(row["delivery_status"]),
        resident_id=row.get("resident_id"),
        resident_name=row.get("resident_name"),
        resident_avatar_ref=row.get("resident_avatar_ref"),
        title=row.get("title"),
        body_text=row.get("body_text"),
        target_type=str(row.get("target_type") or "none"),
        target_id=row.get("target_id"),
        delivered_at=row.get("delivered_at"),
        read_at=row.get("read_at"),
        expires_at=row.get("expires_at"),
    )


class SqlMingchanNotificationRepository:
    """固定 ``app_id=mingchan`` 的通知读取与已读 adapter。"""

    def list_visible(
        self,
        *,
        platform_user_id: str,
        now: str,
        unread_only: bool,
        cursor_delivered_at: Optional[str],
        cursor_notification_id: Optional[str],
        limit: int,
    ) -> Sequence[MingchanNotificationRecord]:
        """列出该真人在鸣蝉内的有效通知。"""

        return tuple(
            _notification(row)
            for row in notification_db.list_app_notifications(
                platform_user_id=platform_user_id,
                app_id=MINGCHAN_APP_ID,
                now=now,
                unread_only=unread_only,
                cursor_delivered_at=cursor_delivered_at,
                cursor_notification_id=cursor_notification_id,
                limit=limit,
            )
        )

    def count_unread(self, *, platform_user_id: str, now: str) -> int:
        """统计该真人在鸣蝉内的有效未读通知。"""

        return notification_db.count_unread_app_notifications(
            platform_user_id=platform_user_id,
            app_id=MINGCHAN_APP_ID,
            now=now,
        )

    def mark_read(
        self,
        *,
        platform_user_id: str,
        notification_id: str,
        now: str,
        read_expires_at: str,
    ) -> Optional[MingchanNotificationRecord]:
        """只在鸣蝉产品作用域内标记单条通知已读。"""

        row = notification_db.mark_app_notification_read(
            notification_id=notification_id,
            platform_user_id=platform_user_id,
            app_id=MINGCHAN_APP_ID,
            now=now,
            read_expires_at=read_expires_at,
        )
        if row is None:
            return None
        projected = notification_db.get_app_notification_for_owner(
            notification_id=notification_id,
            platform_user_id=platform_user_id,
            app_id=MINGCHAN_APP_ID,
        )
        return _notification(projected) if projected else None

    def mark_all_read(
        self, *, platform_user_id: str, now: str, read_expires_at: str
    ) -> Tuple[int, str]:
        """只标记该真人在鸣蝉内的全部通知已读。"""

        return notification_db.mark_all_app_notifications_read(
            platform_user_id=platform_user_id,
            app_id=MINGCHAN_APP_ID,
            now=now,
            read_expires_at=read_expires_at,
        )


__all__ = ["SqlMingchanNotificationRepository"]
