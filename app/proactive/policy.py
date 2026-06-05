from dataclasses import dataclass, field
from datetime import datetime, timedelta, time
from enum import Enum
from typing import Any, Dict, Optional

from app.config import settings
from app.db import (
    count_reactivation_outbound_for_quota_date,
    get_account,
    get_content_invitation_preference,
    get_outbound_daily_usage,
    get_pending_companion_followup_count_in_window,
    get_pending_reminder_count_in_window,
)


POLICY_VERSION = "outbound_policy_v1"


class OutboundCategory(str, Enum):
    USER_REMINDER = "user_reminder"
    COMPANION_FOLLOWUP = "companion_followup"
    CONTENT_INVITATION = "content_invitation"
    REACTIVATION_TOPIC_FOLLOWUP = "reactivation_topic_followup"
    REACTIVATION_CONTENT_INVITATION = "reactivation_content_invitation"
    CONTENT_INVITATION_RESPONSE = "content_invitation_response"
    TASK_RESULT = "task_result"
    LEGACY_PROACTIVE = "legacy_proactive"


SOURCE_CATEGORY_MAP = {
    "reminder": OutboundCategory.USER_REMINDER,
    "reminder_change_confirmation": OutboundCategory.USER_REMINDER,
    "commitment": OutboundCategory.COMPANION_FOLLOWUP,
    "account_check": OutboundCategory.COMPANION_FOLLOWUP,
    "heartbeat": OutboundCategory.COMPANION_FOLLOWUP,
    "content_invitation": OutboundCategory.CONTENT_INVITATION,
    "content_invitation_titles": OutboundCategory.CONTENT_INVITATION_RESPONSE,
    "content_invitation_feedback": OutboundCategory.CONTENT_INVITATION_RESPONSE,
    "async_task_result": OutboundCategory.TASK_RESULT,
}


@dataclass
class PolicyDecision:
    allowed: bool
    status: str
    reason: Optional[str]
    quota_date: str
    category: OutboundCategory
    counts: Dict[str, Any] = field(default_factory=dict)
    next_allowed_at: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


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


def normalize_outbound_category(
    *,
    source: str,
    product_category: Optional[str] = None,
) -> OutboundCategory:
    raw_category = str(product_category or "").strip()
    if raw_category:
        try:
            return OutboundCategory(raw_category)
        except ValueError:
            return OutboundCategory.LEGACY_PROACTIVE
    return SOURCE_CATEGORY_MAP.get(str(source or "").strip(), OutboundCategory.LEGACY_PROACTIVE)


def _category_daily_limit(category: OutboundCategory) -> int:
    def _setting_int(name: str, default: int) -> int:
        value = getattr(settings, name, default)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return default

    if category == OutboundCategory.COMPANION_FOLLOWUP:
        return _setting_int("companion_followup_daily_limit", 1)
    if category == OutboundCategory.CONTENT_INVITATION:
        return _setting_int("content_invitation_daily_limit", 1)
    if category in {
        OutboundCategory.REACTIVATION_TOPIC_FOLLOWUP,
        OutboundCategory.REACTIVATION_CONTENT_INVITATION,
    }:
        return _setting_int("reactivation_daily_limit", 1)
    if category == OutboundCategory.LEGACY_PROACTIVE:
        return _setting_int("proactive_outbound_daily_limit", 0)
    return 0


def _blocked(
    *,
    quota_date: str,
    category: OutboundCategory,
    reason: str,
    counts: Optional[Dict[str, Any]] = None,
    next_allowed_at: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> PolicyDecision:
    return PolicyDecision(
        allowed=False,
        status="cancelled",
        reason=reason,
        quota_date=quota_date,
        category=category,
        counts=counts or {},
        next_allowed_at=next_allowed_at,
        metadata=metadata or {},
    )


def _allowed(
    *,
    quota_date: str,
    category: OutboundCategory,
    counts: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> PolicyDecision:
    return PolicyDecision(
        allowed=True,
        status="pending",
        reason=None,
        quota_date=quota_date,
        category=category,
        counts=counts or {},
        metadata=metadata or {},
    )


def evaluate_outbound_policy(
    *,
    account_id: str,
    category: OutboundCategory,
    source: str,
    scheduled_at: Optional[datetime],
    now: datetime,
    metadata: Optional[Dict[str, Any]] = None,
) -> PolicyDecision:
    quota_date = now.date().isoformat()
    policy_metadata = {
        "policy_version": POLICY_VERSION,
        "policy_checked_at": now.isoformat(timespec="seconds"),
        "source": source,
        **(metadata or {}),
    }

    account = get_account(account_id=account_id)
    if account is None:
        return _blocked(
            quota_date=quota_date,
            category=category,
            reason="account_not_found",
            metadata=policy_metadata,
        )
    if account.get("status") != "active":
        return _blocked(
            quota_date=quota_date,
            category=category,
            reason="account_not_active",
            metadata=policy_metadata,
        )

    if category in {
        OutboundCategory.USER_REMINDER,
        OutboundCategory.CONTENT_INVITATION_RESPONSE,
        OutboundCategory.TASK_RESULT,
    }:
        return _allowed(quota_date=quota_date, category=category, metadata=policy_metadata)

    if not getattr(settings, "proactive_outbound_enabled", True):
        return _blocked(
            quota_date=quota_date,
            category=category,
            reason="proactive_outbound_disabled",
            metadata=policy_metadata,
        )

    bypass_quiet_hours = bool((metadata or {}).get("bypass_quiet_hours"))
    if not bypass_quiet_hours and is_quiet_hours(
        now=now,
        start=getattr(settings, "proactive_quiet_hours_start", "22:00"),
        end=getattr(settings, "proactive_quiet_hours_end", "08:00"),
    ):
        return _blocked(
            quota_date=quota_date,
            category=category,
            reason="quiet_hours",
            metadata=policy_metadata,
        )

    counts: Dict[str, Any] = {}
    daily_limit = _category_daily_limit(category)
    if daily_limit > 0:
        if category in {
            OutboundCategory.REACTIVATION_TOPIC_FOLLOWUP,
            OutboundCategory.REACTIVATION_CONTENT_INVITATION,
        }:
            current_count = count_reactivation_outbound_for_quota_date(
                account_id=account_id,
                quota_date=quota_date,
            )
        else:
            current_count = get_outbound_daily_usage(
                account_id=account_id,
                quota_date=quota_date,
                product_category=category.value,
            )
        counts["daily_count"] = current_count
        counts["daily_limit"] = daily_limit
        if current_count >= daily_limit:
            return _blocked(
                quota_date=quota_date,
                category=category,
                reason="daily_limit_exceeded",
                counts=counts,
                metadata=policy_metadata,
            )

    if category in {
        OutboundCategory.CONTENT_INVITATION,
        OutboundCategory.REACTIVATION_CONTENT_INVITATION,
    }:
        topic = str((metadata or {}).get("topic") or "").strip()
        if topic:
            preference = get_content_invitation_preference(
                account_id=account_id,
                topic=topic,
            )
            if preference:
                pref_status = str(preference.get("status") or "").strip()
                cooldown_until = str(preference.get("cooldown_until") or "").strip()
                if pref_status == "blocked":
                    return _blocked(
                        quota_date=quota_date,
                        category=category,
                        reason="content_topic_blocked",
                        counts=counts,
                        metadata=policy_metadata,
                    )
                if pref_status == "cooled_down" and cooldown_until and cooldown_until > now.strftime("%Y-%m-%d %H:%M:%S"):
                    return _blocked(
                        quota_date=quota_date,
                        category=category,
                        reason="content_topic_cooldown",
                        counts=counts,
                        next_allowed_at=cooldown_until,
                        metadata=policy_metadata,
                    )

    if category in {
        OutboundCategory.COMPANION_FOLLOWUP,
        OutboundCategory.CONTENT_INVITATION,
        OutboundCategory.REACTIVATION_TOPIC_FOLLOWUP,
        OutboundCategory.REACTIVATION_CONTENT_INVITATION,
    }:
        try:
            avoidance_hours = int(getattr(settings, "proactive_avoidance_window_hours", 6) or 0)
        except (TypeError, ValueError):
            avoidance_hours = 6
        if avoidance_hours > 0:
            window_start = now
            window_end = now + timedelta(hours=avoidance_hours)
            reminder_count = get_pending_reminder_count_in_window(
                account_id=account_id,
                start_at=window_start.strftime("%Y-%m-%d %H:%M:%S"),
                end_at=window_end.strftime("%Y-%m-%d %H:%M:%S"),
            )
            counts["avoidance_user_reminder_count"] = reminder_count
            if reminder_count > 0:
                return _blocked(
                    quota_date=quota_date,
                    category=category,
                    reason="avoidance_window_user_reminder",
                    counts=counts,
                    next_allowed_at=window_end.strftime("%Y-%m-%d %H:%M:%S"),
                    metadata=policy_metadata,
                )
            if category in {
                OutboundCategory.CONTENT_INVITATION,
                OutboundCategory.REACTIVATION_CONTENT_INVITATION,
            }:
                companion_count = get_pending_companion_followup_count_in_window(
                    account_id=account_id,
                    start_at=window_start.strftime("%Y-%m-%d %H:%M:%S"),
                    end_at=window_end.strftime("%Y-%m-%d %H:%M:%S"),
                )
                counts["avoidance_companion_followup_count"] = companion_count
                if companion_count > 0:
                    return _blocked(
                        quota_date=quota_date,
                        category=category,
                        reason="avoidance_window_companion_followup",
                        counts=counts,
                        next_allowed_at=window_end.strftime("%Y-%m-%d %H:%M:%S"),
                        metadata=policy_metadata,
                    )

    return _allowed(
        quota_date=quota_date,
        category=category,
        counts=counts,
        metadata=policy_metadata,
    )
