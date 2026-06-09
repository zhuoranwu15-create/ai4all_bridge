from dataclasses import dataclass, field
from datetime import datetime, timedelta, time
from enum import Enum
from typing import Any, Dict, Optional

from app.config import settings
from app.db import (
    count_outbound_in_window,
    count_reactivation_outbound_for_quota_date,
    count_reactivation_outbound_in_window,
    count_total_proactive_outbound_for_quota_date,
    get_account,
    get_content_invitation_preference,
    get_outbound_daily_usage,
    get_pending_companion_followup_count_in_window,
    get_pending_reminder_count_in_window,
)
from app.proactive.settings import (
    category_to_frequency_bucket,
    get_effective_proactive_message_settings,
    get_total_daily_limit,
    is_category_enabled,
    is_in_allowed_window,
    resolve_frequency_limits,
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
    # content_invitation sends now flow through the unified reactivation path and
    # share its single daily limit; the legacy CONTENT_INVITATION category maps to
    # the same limit so any residual legacy-category send stays bounded.
    if category in {
        OutboundCategory.CONTENT_INVITATION,
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

    # 账号级主动消息设置（用户偏好）。在豁免分类之后读取，提醒等不受影响。
    eff_settings = get_effective_proactive_message_settings(account_id)

    # 用户总开关关闭：拦截全部非豁免主动消息。
    if not eff_settings.get("master_enabled", True):
        return _blocked(
            quota_date=quota_date,
            category=category,
            reason="proactive_master_disabled",
            metadata=policy_metadata,
        )

    # 用户关闭了当前分类。
    if not is_category_enabled(eff_settings, category.value):
        return _blocked(
            quota_date=quota_date,
            category=category,
            reason="proactive_category_disabled",
            metadata=policy_metadata,
        )

    # 临时静默窗口。
    muted_until = str(eff_settings.get("muted_until") or "").strip()
    if muted_until and muted_until > now.strftime("%Y-%m-%d %H:%M:%S"):
        return _blocked(
            quota_date=quota_date,
            category=category,
            reason="proactive_muted",
            next_allowed_at=muted_until,
            metadata=policy_metadata,
        )

    # 全局开关（兜底；用户未显式设置时也生效）。
    if not getattr(settings, "proactive_outbound_enabled", True):
        return _blocked(
            quota_date=quota_date,
            category=category,
            reason="proactive_outbound_disabled",
            metadata=policy_metadata,
        )

    # 允许推送时段窗口：用户设了窗口而当前不在任一窗口内 → 拦截。空窗口=不限制。
    allowed_windows = eff_settings.get("allowed_windows") or []
    if allowed_windows and not is_in_allowed_window(now, allowed_windows):
        return _blocked(
            quota_date=quota_date,
            category=category,
            reason="proactive_outside_allowed_window",
            metadata=policy_metadata,
        )

    # 静默时段：优先用用户自定义，否则用全局。用户设置命中时用专属 reason。
    bypass_quiet_hours = bool((metadata or {}).get("bypass_quiet_hours"))
    eff_quiet = eff_settings.get("quiet_hours") or {}
    if (
        bool(eff_quiet.get("enabled", True))
        and not bypass_quiet_hours
        and is_quiet_hours(
            now=now,
            start=eff_quiet.get("start") or getattr(settings, "proactive_quiet_hours_start", "22:00"),
            end=eff_quiet.get("end") or getattr(settings, "proactive_quiet_hours_end", "08:00"),
        )
    ):
        return _blocked(
            quota_date=quota_date,
            category=category,
            reason=(
                "proactive_user_quiet_hours"
                if eff_settings.get("quiet_hours_is_user")
                else "quiet_hours"
            ),
            metadata=policy_metadata,
        )

    counts: Dict[str, Any] = {}
    # 频次桶 + 用户自定义频次（已 clamp 到系统硬上限）。
    bucket = category_to_frequency_bucket(category.value)
    freq_limits = resolve_frequency_limits(eff_settings, bucket) if bucket else {}
    is_reactivation_bucket = category in {
        OutboundCategory.CONTENT_INVITATION,
        OutboundCategory.REACTIVATION_TOPIC_FOLLOWUP,
        OutboundCategory.REACTIVATION_CONTENT_INVITATION,
    }

    # 日上限：用户设了就用用户值（覆盖全局，可放宽/收紧），否则沿用全局。
    user_daily = freq_limits.get("max_per_day")
    daily_is_user = user_daily is not None
    daily_limit = user_daily if daily_is_user else _category_daily_limit(category)
    if daily_limit > 0:
        if is_reactivation_bucket:
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
                reason="proactive_user_frequency_exceeded" if daily_is_user else "daily_limit_exceeded",
                counts=counts,
                metadata=policy_metadata,
            )

    # 周上限（仅用户设置时生效）：滚动最近 7 天计数。
    weekly_limit = freq_limits.get("max_per_week")
    if weekly_limit:
        since = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        if is_reactivation_bucket:
            weekly_count = count_reactivation_outbound_in_window(
                account_id=account_id,
                since=since,
            )
        else:
            weekly_count = count_outbound_in_window(
                account_id=account_id,
                product_category=category.value,
                since=since,
            )
        counts["weekly_count"] = weekly_count
        counts["weekly_limit"] = weekly_limit
        if weekly_count >= weekly_limit:
            return _blocked(
                quota_date=quota_date,
                category=category,
                reason="proactive_user_frequency_exceeded",
                counts=counts,
                metadata=policy_metadata,
            )

    # 全局每日总量上限（所有非豁免分类合计）。提醒/task_result 等已在 fast-path 豁免，不计入。
    total_daily_limit = get_total_daily_limit(eff_settings)
    if total_daily_limit is not None:
        total_count = count_total_proactive_outbound_for_quota_date(
            account_id=account_id,
            quota_date=quota_date,
        )
        counts["total_daily_count"] = total_count
        counts["total_daily_limit"] = total_daily_limit
        if total_count >= total_daily_limit:
            return _blocked(
                quota_date=quota_date,
                category=category,
                reason="proactive_user_frequency_exceeded",
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
