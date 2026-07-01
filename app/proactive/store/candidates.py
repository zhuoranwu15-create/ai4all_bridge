"""自主外联候选的**存储层**：持久化（account_state metadata）+ 归一化 + 去重规则。

从 reactivation.py 拆出"候选怎么存、怎么读、怎么去重"这一职责，与"何时发"（scheduling.py）
和"取到期候选→发送"（delivery/dispatch.py）分家。账号隔离不变量在此层落地：所有读写都按
account_id 约束，候选存在该账号 proactive_account_state 的 metadata JSON 里。

本模块公共符号供 recall/delivery/orchestration 各层直接 import。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app.config import settings
from app.db import (
    count_reactivation_outbound_for_quota_date,
    count_recent_inbound_messages_for_account,
    get_pending_companion_followup_count_in_window,
    get_pending_reminder_count_in_window,
    get_proactive_account_state,
    list_recent_reactivation_outbound_messages,
    upsert_proactive_account_state,
)
from app.proactive.contract.common import format_reactivation_time


REACTIVATION_METADATA_KEY = "reactivation_candidate"
REACTIVATION_TYPE_TOPIC_FOLLOWUP = "topic_followup"
REACTIVATION_TYPE_CONTENT_INVITATION = "content_invitation"
REACTIVATION_TYPES = {
    REACTIVATION_TYPE_TOPIC_FOLLOWUP,
    REACTIVATION_TYPE_CONTENT_INVITATION,
}

# 拉活分类已合并：话题唤回归入 companion_followup，内容唤回归入 content_invitation。
# 拉活来源仍由 outbound metadata 的 reactivation=True 标志识别（见 reactivation_outbound_metadata）。
REACTIVATION_PRODUCT_CATEGORY_TOPIC_FOLLOWUP = "companion_followup"
REACTIVATION_PRODUCT_CATEGORY_CONTENT_INVITATION = "content_invitation"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _clean_optional_text(value: Any) -> Optional[str]:
    text = _clean_text(value)
    return text or None


def _parse_reactivation_time(value: Any) -> Optional[datetime]:
    text = _clean_text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None


def _normalize_confidence(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(confidence, 1.0))


def normalize_reactivation_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize a reactivation candidate for account metadata storage."""
    candidate_type = _clean_text(candidate.get("type"))
    if candidate_type not in REACTIVATION_TYPES:
        raise ValueError("reactivation candidate type is invalid")

    text = _clean_text(candidate.get("text"))
    if not text:
        raise ValueError("reactivation candidate text is required")

    candidate_id = _clean_text(candidate.get("id"))
    if not candidate_id:
        raise ValueError("reactivation candidate id is required")

    normalized: Dict[str, Any] = {
        "id": candidate_id,
        "type": candidate_type,
        "text": text[:240],
    }

    for key in (
        "topic",
        "reason",
        "generated_at",
        "scheduled_slot",
        "scheduled_at",
        "content_invitation_id",
    ):
        value = _clean_optional_text(candidate.get(key))
        if value:
            normalized[key] = value

    confidence = _normalize_confidence(candidate.get("confidence"))
    if confidence is not None:
        normalized["confidence"] = confidence

    source_message_cutoff_id = candidate.get("source_message_cutoff_id")
    if source_message_cutoff_id is not None:
        try:
            normalized["source_message_cutoff_id"] = int(source_message_cutoff_id)
        except (TypeError, ValueError):
            raise ValueError("source_message_cutoff_id must be an integer") from None

    for key in ("dedupe", "policy", "metadata"):
        value = candidate.get(key)
        if isinstance(value, dict):
            normalized[key] = value

    if candidate_type == REACTIVATION_TYPE_CONTENT_INVITATION and not normalized.get(
        "content_invitation_id"
    ):
        raise ValueError("content_invitation_id is required for content_invitation reactivation")

    return normalized


def get_reactivation_candidate_from_metadata(
    metadata: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Return a normalized reactivation candidate from proactive metadata, if present."""
    raw = (metadata or {}).get(REACTIVATION_METADATA_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        return normalize_reactivation_candidate(raw)
    except ValueError:
        return None


def get_reactivation_candidate(*, account_id: str) -> Optional[Dict[str, Any]]:
    """Read the current reactivation candidate for one account."""
    state = get_proactive_account_state(account_id=account_id)
    if state is None:
        return None
    return get_reactivation_candidate_from_metadata(state.get("metadata") or {})


def upsert_reactivation_candidate(
    *,
    account_id: str,
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    """Store or replace the current reactivation candidate for one account.

    Uses an atomic json_patch so concurrent writers (scheduler + admin
    run-once) cannot silently drop sibling metadata keys.
    """
    normalized = normalize_reactivation_candidate(candidate)
    return upsert_proactive_account_state(
        account_id=account_id,
        metadata_patch={REACTIVATION_METADATA_KEY: normalized},
    )


def clear_reactivation_candidate(
    *,
    account_id: str,
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Remove the current reactivation candidate while preserving other metadata.

    Uses json_patch null-deletion semantics; only the named keys are touched.
    """
    patch: Dict[str, Any] = {REACTIVATION_METADATA_KEY: None}
    if reason:
        patch["reactivation_candidate_cleared_reason"] = reason
    if now:
        patch["reactivation_candidate_cleared_at"] = format_reactivation_time(now)
    return upsert_proactive_account_state(
        account_id=account_id,
        metadata_patch=patch,
    )


def reschedule_reactivation_candidate(
    *,
    account_id: str,
    candidate: Dict[str, Any],
    scheduled_slot: str,
    scheduled_at: str,
    reason: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Move a candidate to a later send slot."""
    next_candidate = dict(candidate)
    policy = dict(next_candidate.get("policy") or {})
    policy["last_reschedule_reason"] = reason
    if now:
        policy["last_rescheduled_at"] = format_reactivation_time(now)
    next_candidate["scheduled_slot"] = scheduled_slot
    next_candidate["scheduled_at"] = scheduled_at
    next_candidate["policy"] = policy
    return upsert_reactivation_candidate(account_id=account_id, candidate=next_candidate)


def reactivation_outbound_metadata(
    *,
    candidate: Dict[str, Any],
    sent_at: Optional[datetime] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build structured outbound metadata for sent reactivation messages."""
    normalized = normalize_reactivation_candidate(candidate)
    metadata: Dict[str, Any] = {
        "reactivation": True,
        "reactivation_type": normalized["type"],
        "reactivation_candidate_id": normalized["id"],
        "topic": normalized.get("topic"),
        "planning_generated_at": normalized.get("generated_at"),
        "scheduled_slot": normalized.get("scheduled_slot"),
        "scheduled_at": normalized.get("scheduled_at"),
        "source_message_cutoff_id": normalized.get("source_message_cutoff_id"),
        "dedupe": normalized.get("dedupe") or {},
    }
    if normalized["type"] == REACTIVATION_TYPE_CONTENT_INVITATION:
        metadata["content_invitation_id"] = normalized.get("content_invitation_id")
    if sent_at:
        metadata["sent_at"] = format_reactivation_time(sent_at)
    if extra:
        metadata.update(extra)
    return {key: value for key, value in metadata.items() if value is not None}


def _reactivation_product_category(candidate_type: str) -> str:
    if candidate_type == REACTIVATION_TYPE_CONTENT_INVITATION:
        return REACTIVATION_PRODUCT_CATEGORY_CONTENT_INVITATION
    return REACTIVATION_PRODUCT_CATEGORY_TOPIC_FOLLOWUP


def _has_sent_reactivation_today(*, account_id: str, now: datetime) -> bool:
    # Use quota_date (local-day key set at insert time) instead of created_at
    # so the daily limit is correct regardless of server timezone vs UTC
    # CURRENT_TIMESTAMP. Goes through the product_category index.
    return (
        count_reactivation_outbound_for_quota_date(
            account_id=account_id,
            quota_date=now.date().isoformat(),
        )
        > 0
    )


def _inbound_count_after(*, account_id: str, after: datetime) -> int:
    """``after`` 之后（严格大于、不含同秒）该账号的入站消息数。

    仅统计 ``direction='inbound'``（用户真正说的话），不含 bot 自身/提醒等出站，
    用于判断「候选生成后用户是否又说过话」。

    时区一致性（关键）：``after``（候选 generated_at）与 ``messages.created_at`` **同为
    北京时间字符串**（created_at 由 insert 显式写 ``datetime('now','+8 hours')``），因此
    直接按北京时间比较，**不做 UTC 转换**。曾用 ``local_to_utc_string`` 把阈值 -8h，导致
    生成前 8 小时内的入站被误算为「生成后」而误清候选。``+1s`` 在秒粒度上实现严格 ``>``，
    排除与候选生成同一秒的源消息。
    """
    threshold = format_reactivation_time(after + timedelta(seconds=1))
    return count_recent_inbound_messages_for_account(
        account_id=account_id,
        since=threshold,
    )


def _avoidance_count(*, account_id: str, now: datetime) -> int:
    try:
        minutes = int(getattr(settings, "reactivation_avoidance_window_minutes", 60) or 60)
    except (TypeError, ValueError):
        minutes = 60
    end = now + timedelta(minutes=max(minutes, 1))
    start_text = format_reactivation_time(now)
    end_text = format_reactivation_time(end)
    return (
        get_pending_reminder_count_in_window(
            account_id=account_id,
            start_at=start_text,
            end_at=end_text,
        )
        + get_pending_companion_followup_count_in_window(
            account_id=account_id,
            start_at=start_text,
            end_at=end_text,
        )
    )


def _normalize_dedupe_key(value: Any) -> str:
    """归一化 topic/文案用于精确比较：小写 + 去首尾与内部多余空白。"""
    text = _clean_text(value)
    if not text:
        return ""
    return " ".join(text.lower().split())


def rule_reactivation_dedupe_check(
    *,
    account_id: str,
    candidate: Dict[str, Any],
    now: datetime,
) -> Dict[str, Any]:
    """零成本去重兜底：与近 N 天已发拉活做精确 topic/文案匹配，不调 LLM。

    语义去重（避免主题/问法雷同）已在候选**生成**时由生成器 prompt 处理
    （recent_sent_topics 注入）。这里只保留一层 O(1) 的精确匹配防线，挡住完全相同的
    topic 或文案被重复发出；不试图做相似度判断，故不需要 LLM。
    """
    try:
        lookback_days = int(getattr(settings, "reactivation_dedupe_days", 3) or 3)
    except (TypeError, ValueError):
        lookback_days = 3
    since = now - timedelta(days=max(lookback_days, 1))
    # outbound_messages.created_at 存北京时间，now 也是北京 naive 时间，直接按北京时间比较，
    # 不做 UTC 转换（旧 local_to_utc_string 会把阈值偏移一个时区）。
    history = list_recent_reactivation_outbound_messages(
        account_id=account_id,
        since=format_reactivation_time(since),
        limit=20,
    )
    if not history:
        return {
            "checked": True,
            "duplicate": False,
            "reason": "no_recent_reactivation_history",
            "lookback_days": lookback_days,
        }

    candidate_topic = _normalize_dedupe_key(candidate.get("topic"))
    candidate_text = _normalize_dedupe_key(candidate.get("text"))
    for item in history:
        metadata = item.get("metadata") or {}
        if candidate_topic and candidate_topic == _normalize_dedupe_key(metadata.get("topic")):
            return {
                "checked": True,
                "duplicate": True,
                "reason": "duplicate_topic",
                "matched_outbound_id": item.get("id"),
                "lookback_days": lookback_days,
            }
        if candidate_text and candidate_text == _normalize_dedupe_key(item.get("text")):
            return {
                "checked": True,
                "duplicate": True,
                "reason": "duplicate_text",
                "matched_outbound_id": item.get("id"),
                "lookback_days": lookback_days,
            }
    return {
        "checked": True,
        "duplicate": False,
        "reason": "rule_dedupe_checked",
        "lookback_days": lookback_days,
    }
