"""鸣蝉 App inbox 主动通知的产品级投递入口。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from app.products.mingchan.infrastructure.app_inbox import (
    AppInboxAdapter,
    AppInboxIntent,
)
from app.time_utils import beijing_naive_now


def dispatch_resident_notification(
    *,
    runtime_account_id: str,
    source: str,
    text: str,
    idempotency_key: str,
    product_category: str,
    now: Optional[datetime] = None,
    source_id: Optional[str] = None,
    target_type: str = "conversation",
    target_id: Optional[str] = None,
) -> Dict[str, Any]:
    """按鸣蝉产品偏好向 resident 对应真人投递一条幂等 App 通知。

    App inbox 是拉取式产品资产，不经过微信 gateway/outbound ledger。被产品开关、
    World 状态或用户安静偏好阻断时返回 ``cancelled``，不会创建通知行。
    """

    adapter = AppInboxAdapter()
    reason = adapter.delivery_block_reason(runtime_account_id)
    if reason is not None:
        return {
            "status": "cancelled",
            "error": reason,
            "runtime_account_id": runtime_account_id,
        }
    item, created = adapter.deliver(
        AppInboxIntent(
            runtime_account_id=runtime_account_id,
            category=product_category,
            source_type=source,
            source_id=source_id,
            idempotency_key=idempotency_key,
            body_text=text,
            target_type=target_type,
            target_id=target_id,
        ),
        now=now or beijing_naive_now(),
    )
    return {
        "status": "sent",
        "error": None,
        "notification_id": item.id,
        "created": created,
        "runtime_account_id": runtime_account_id,
    }


__all__ = ["dispatch_resident_notification"]
