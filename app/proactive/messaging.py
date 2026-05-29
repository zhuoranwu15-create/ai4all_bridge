from datetime import datetime, time
from typing import Any, Dict, Optional

from app.config import settings
from app.db import (
    claim_pending_outbound_message,
    create_outbound_message,
    get_outbound_daily_usage,
    mark_outbound_message_failed,
    mark_outbound_message_sent,
)
from app.openclaw_gateway import send_weixin_text


def _parse_hhmm(value: str) -> time:
    text = str(value or "").strip()
    hour_text, minute_text = text.split(":", 1)
    hour = int(hour_text)
    minute = int(minute_text)
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("time must be in HH:MM format")
    return time(hour=hour, minute=minute)


def is_quiet_hours(
    *,
    now: datetime,
    start: str,
    end: str,
) -> bool:
    start_time = _parse_hhmm(start)
    end_time = _parse_hhmm(end)
    if start_time == end_time:
        return False
    current = now.time()
    if start_time < end_time:
        return start_time <= current < end_time
    return current >= start_time or current < end_time


_BYPASS_QUIET_HOURS_CATEGORIES = {"user_reminder", "task_result"}
_UNLIMITED_CATEGORIES = {"user_reminder", "task_result"}


def enqueue_proactive_text(
    *,
    account_id: str,
    channel: str,
    channel_account_id: Optional[str],
    to_user_id: str,
    session_key: Optional[str],
    source: str,
    text: str,
    idempotency_key: Optional[str] = None,
    now: Optional[datetime] = None,
    bypass_quiet_hours: bool = False,
    product_category: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    current = now or datetime.now()
    quota_date = current.date().isoformat()
    status = "pending"
    error = None
    merged_metadata = {
        **(metadata or {}),
        "policy_checked_at": current.isoformat(timespec="seconds"),
    }
    if product_category:
        merged_metadata["product_category"] = product_category

    effective_bypass = bypass_quiet_hours or (product_category in _BYPASS_QUIET_HOURS_CATEGORIES)
    skip_daily_limit = product_category in _UNLIMITED_CATEGORIES

    if not getattr(settings, "proactive_outbound_enabled", True):
        if product_category != "user_reminder":
            status = "cancelled"
            error = "proactive_outbound_disabled"

    if status == "pending" and not effective_bypass and is_quiet_hours(
        now=current,
        start=getattr(settings, "proactive_quiet_hours_start", "22:00"),
        end=getattr(settings, "proactive_quiet_hours_end", "08:00"),
    ):
        status = "cancelled"
        error = "quiet_hours"

    if status == "pending" and not skip_daily_limit:
        max_per_day = int(getattr(settings, "proactive_outbound_daily_limit", 0) or 0)
        if max_per_day > 0:
            current_count = get_outbound_daily_usage(
                account_id=account_id,
                quota_date=quota_date,
            )
            if current_count >= max_per_day:
                status = "cancelled"
                error = "daily_limit_exceeded"
                merged_metadata["daily_count"] = current_count
                merged_metadata["daily_limit"] = max_per_day

    if error:
        merged_metadata["policy_error"] = error

    return create_outbound_message(
        account_id=account_id,
        channel=channel,
        channel_account_id=channel_account_id,
        to_user_id=to_user_id,
        session_key=session_key,
        source=source,
        text=text,
        idempotency_key=idempotency_key,
        quota_date=quota_date,
        status=status,
        error=error,
        metadata=merged_metadata,
    )


def send_proactive_text(
    *,
    account_id: str,
    channel: str,
    channel_account_id: Optional[str],
    to_user_id: str,
    session_key: Optional[str],
    source: str,
    text: str,
    idempotency_key: Optional[str] = None,
    now: Optional[datetime] = None,
    bypass_quiet_hours: bool = False,
    product_category: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    outbound = enqueue_proactive_text(
        account_id=account_id,
        channel=channel,
        channel_account_id=channel_account_id,
        to_user_id=to_user_id,
        session_key=session_key,
        source=source,
        text=text,
        idempotency_key=idempotency_key,
        now=now,
        bypass_quiet_hours=bypass_quiet_hours,
        product_category=product_category,
        metadata=metadata,
    )
    if outbound["status"] != "pending":
        return outbound

    claimed = claim_pending_outbound_message(outbound_message_id=int(outbound["id"]))
    if claimed is None:
        return outbound

    try:
        result = send_weixin_text(
            to_user_id=to_user_id,
            text=text,
            gateway_timeout_ms=settings.openclaw_gateway_call_timeout_ms,
            account_id=channel_account_id,
            idempotency_key=claimed["idempotency_key"],
            session_key=session_key,
            channel=channel,
        )
    except Exception as err:
        failed = mark_outbound_message_failed(
            outbound_message_id=int(claimed["id"]),
            error=str(err),
        )
        if failed is None:
            raise
        return failed

    gateway_message_id = result.get("messageId") if isinstance(result, dict) else None
    return mark_outbound_message_sent(
        outbound_message_id=int(claimed["id"]),
        gateway_message_id=str(gateway_message_id) if gateway_message_id else None,
    ) or claimed
