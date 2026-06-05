from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from app.config import settings
from app.db import (
    claim_content_invitation_for_send,
    count_reactivation_outbound_for_quota_date,
    count_recent_inbound_messages_for_account,
    get_latest_message_id_for_account,
    get_pending_companion_followup_count_in_window,
    get_pending_reminder_count_in_window,
    get_proactive_account_state,
    list_channel_bindings_for_account,
    list_due_reactivation_candidate_accounts,
    list_recent_reactivation_outbound_messages,
    mark_content_invitation_invited,
    release_content_invitation_claim,
    upsert_proactive_account_state,
)
from app.proactive.messaging import send_proactive_text


REACTIVATION_METADATA_KEY = "reactivation_candidate"
REACTIVATION_TYPE_TOPIC_FOLLOWUP = "topic_followup"
REACTIVATION_TYPE_CONTENT_INVITATION = "content_invitation"
REACTIVATION_TYPES = {
    REACTIVATION_TYPE_TOPIC_FOLLOWUP,
    REACTIVATION_TYPE_CONTENT_INVITATION,
}

REACTIVATION_PRODUCT_CATEGORY_TOPIC_FOLLOWUP = "reactivation_topic_followup"
REACTIVATION_PRODUCT_CATEGORY_CONTENT_INVITATION = "reactivation_content_invitation"

ReactivationGenerator = Callable[..., Dict[str, Any]]
DedupeChecker = Callable[..., Dict[str, Any]]
Regenerator = Callable[..., Dict[str, Any]]


def format_reactivation_time(value: datetime) -> str:
    """Format reactivation timestamps consistently with proactive state metadata."""
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def local_to_utc_string(value: datetime) -> str:
    """Format a naive local-time datetime as a UTC-naive SQLite-comparable string.

    SQLite stores `created_at` via CURRENT_TIMESTAMP (UTC). Python's
    datetime.now() returns server-local time. Comparing the two without
    conversion skews windows by the local UTC offset. Aware datetimes are
    converted directly; naive datetimes are assumed to be system local.
    """
    if value.tzinfo is None:
        value = value.astimezone()
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0, tzinfo=None)
        .strftime("%Y-%m-%d %H:%M:%S")
    )


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


def _parse_send_slots(value: Optional[str] = None) -> List[str]:
    raw = value if value is not None else getattr(settings, "reactivation_send_slots", "12:15,18:15,21:05")
    slots: List[str] = []
    for part in str(raw or "").split(","):
        text = part.strip()
        try:
            datetime.strptime(text, "%H:%M")
        except ValueError:
            continue
        slots.append(text)
    return slots or ["12:15", "18:15", "21:05"]


def _slot_datetime(day: datetime, slot: str) -> datetime:
    hour, minute = [int(part) for part in slot.split(":", 1)]
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _apply_send_jitter(scheduled: datetime) -> datetime:
    """Add a small forward random offset to a slot time so sends spread out.

    Forward-only (never before the slot). Configurable via
    reactivation_send_jitter_min/max_seconds; 0/0 disables (used in tests).
    """
    try:
        low = int(getattr(settings, "reactivation_send_jitter_min_seconds", 60) or 0)
        high = int(getattr(settings, "reactivation_send_jitter_max_seconds", 120) or 0)
    except (TypeError, ValueError):
        low, high = 60, 120
    low = max(low, 0)
    high = max(high, 0)
    if high <= 0:
        return scheduled
    if low > high:
        low = high
    return scheduled + timedelta(seconds=random.randint(low, high))


def next_reactivation_slot(
    *,
    now: datetime,
    after_slot: Optional[str] = None,
) -> Dict[str, str]:
    """Return the next configured reactivation send slot at or after now."""
    slots = _parse_send_slots()
    start_index = 0
    if after_slot in slots:
        start_index = slots.index(after_slot) + 1
    for index, slot in enumerate(slots[start_index:], start=start_index):
        scheduled = _slot_datetime(now, slot)
        if scheduled >= now:
            return {
                "scheduled_slot": f"slot_{index + 1}",
                "scheduled_at": format_reactivation_time(_apply_send_jitter(scheduled)),
            }
    first = _slot_datetime(now + timedelta(days=1), slots[0])
    return {
        "scheduled_slot": "slot_1",
        "scheduled_at": format_reactivation_time(_apply_send_jitter(first)),
    }


def _next_slot_after(candidate: Dict[str, Any], *, now: datetime) -> Optional[Dict[str, str]]:
    slots = _parse_send_slots()
    current_slot = _clean_text(candidate.get("scheduled_slot"))
    if current_slot.startswith("slot_"):
        try:
            current_index = int(current_slot.removeprefix("slot_")) - 1
        except ValueError:
            current_index = -1
    else:
        current_index = -1
    next_index = current_index + 1
    if next_index >= len(slots):
        return None
    slot = slots[next_index]
    scheduled = _slot_datetime(now, slot)
    if scheduled < now:
        scheduled = _slot_datetime(now + timedelta(days=1), slot)
    return {
        "scheduled_slot": f"slot_{next_index + 1}",
        "scheduled_at": format_reactivation_time(_apply_send_jitter(scheduled)),
    }


def _with_default_schedule(candidate: Dict[str, Any], *, now: datetime) -> Dict[str, Any]:
    next_candidate = dict(candidate)
    if not _clean_text(next_candidate.get("scheduled_at")):
        next_candidate.update(next_reactivation_slot(now=now))
    return next_candidate


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


def _select_route(account_id: str) -> Optional[Dict[str, Any]]:
    for binding in list_channel_bindings_for_account(account_id=account_id):
        chat_id = _clean_text(binding.get("chat_id"))
        channel_account_id = _clean_text(binding.get("channel_account_id"))
        if chat_id and channel_account_id:
            return {
                "channel_binding_id": binding["id"],
                "channel": binding["channel"],
                "channel_account_id": channel_account_id,
                "to_user_id": chat_id,
                "session_key": binding.get("session_key"),
            }
    return None


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


def _recent_inbound_count(*, account_id: str, now: datetime) -> int:
    try:
        delay_minutes = int(getattr(settings, "reactivation_recent_inbound_delay_minutes", 60) or 60)
    except (TypeError, ValueError):
        delay_minutes = 60
    since = now - timedelta(minutes=max(delay_minutes, 1))
    # messages.created_at is UTC (CURRENT_TIMESTAMP); convert local since -> UTC.
    return count_recent_inbound_messages_for_account(
        account_id=account_id,
        since=local_to_utc_string(since),
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


def _extract_json_object(text: str) -> Dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("LLM output did not contain a JSON object")
    return json.loads(cleaned[start : end + 1])


def llm_reactivation_dedupe_check(
    *,
    account_id: str,
    candidate: Dict[str, Any],
    now: datetime,
    llm_generate: Optional[Callable[[List[Dict[str, str]]], str]] = None,
) -> Dict[str, Any]:
    """Use recent reactivation outbound history to judge whether a candidate repeats."""
    try:
        lookback_days = int(getattr(settings, "reactivation_dedupe_days", 3) or 3)
    except (TypeError, ValueError):
        lookback_days = 3
    since = now - timedelta(days=max(lookback_days, 1))
    # outbound_messages.created_at is UTC; convert local since -> UTC.
    history = list_recent_reactivation_outbound_messages(
        account_id=account_id,
        since=local_to_utc_string(since),
        limit=20,
    )
    if not history:
        return {
            "checked": True,
            "duplicate": False,
            "reason": "no_recent_reactivation_history",
            "lookback_days": lookback_days,
            "retry_count": 0,
        }
    if llm_generate is None:
        from app.llm import generate_completion  # noqa: PLC0415

        llm_generate = generate_completion
    compact_history = [
        {
            "id": item.get("id"),
            "text": item.get("text"),
            "topic": (item.get("metadata") or {}).get("topic"),
            "reactivation_type": (item.get("metadata") or {}).get("reactivation_type"),
            "created_at": item.get("created_at"),
        }
        for item in history
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "你是 reactivation 主动消息去重判断器。只输出 JSON。"
                "判断 new_candidate 是否和 recent_sent 主题、意图或问法过近，"
                "会不会让用户觉得昨天刚问过。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "new_candidate": candidate,
                    "recent_sent": compact_history,
                    "schema": {
                        "duplicate": False,
                        "reason": "简短原因",
                        "matched_outbound_id": None,
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]
    try:
        payload = _extract_json_object(llm_generate(messages))
    except Exception as err:
        return {
            "checked": True,
            "duplicate": False,
            "reason": "dedupe_check_failed_open",
            "error": str(err),
            "lookback_days": lookback_days,
            "retry_count": 0,
        }
    return {
        "checked": True,
        "duplicate": bool(payload.get("duplicate")),
        "reason": _clean_text(payload.get("reason")) or "llm_dedupe_checked",
        "matched_outbound_id": payload.get("matched_outbound_id"),
        "lookback_days": lookback_days,
        "retry_count": 0,
    }


def _no_op(
    *,
    account_id: str,
    reason: str,
    now: datetime,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "action": "no_op",
        "account_id": account_id,
        "reason": reason,
        "evaluated_at": format_reactivation_time(now),
        "metadata": metadata or {},
    }


def _candidate_from_content_invitation(
    *,
    invitation: Dict[str, Any],
    now: datetime,
) -> Dict[str, Any]:
    invitation_id = _clean_text(invitation.get("id"))
    return normalize_reactivation_candidate(
        {
            "id": f"reactivation-content-{invitation_id}",
            "type": REACTIVATION_TYPE_CONTENT_INVITATION,
            "content_invitation_id": invitation_id,
            "topic": invitation.get("topic"),
            "text": invitation.get("invitation_text"),
            "generated_at": format_reactivation_time(now),
            "metadata": {
                "title_count": len(invitation.get("title_items") or []),
                "source": "content_invitation_generation",
            },
        }
    )


def plan_reactivation_candidate(
    *,
    account_id: str,
    now: Optional[datetime] = None,
    topic_followup_generator: Optional[ReactivationGenerator] = None,
    content_invitation_generator: Optional[ReactivationGenerator] = None,
) -> Dict[str, Any]:
    """Refresh the unified reactivation candidate for one account.

    Phase 3 wires the unified planning pass. The topic-followup LLM generator is
    intentionally injectable and still defaults to a no-op until Phase 4.
    """
    current = now or datetime.now()
    topic_result = (
        topic_followup_generator(account_id=account_id, now=current)
        if topic_followup_generator
        else _no_op(
            account_id=account_id,
            reason="topic_followup_generator_not_implemented",
            now=current,
        )
    )
    topic_candidate = topic_result.get("reactivation_candidate")
    if isinstance(topic_candidate, dict):
        state = upsert_reactivation_candidate(
            account_id=account_id,
            candidate=_with_default_schedule(topic_candidate, now=current),
        )
        return {
            "action": "reactivation_candidate_planned",
            "account_id": account_id,
            "reactivation_type": REACTIVATION_TYPE_TOPIC_FOLLOWUP,
            "reactivation_candidate": get_reactivation_candidate_from_metadata(
                state.get("metadata") or {}
            ),
            "topic_followup_generation": topic_result,
            "content_invitation_generation": _no_op(
                account_id=account_id,
                reason="topic_followup_candidate_selected",
                now=current,
            ),
            "evaluated_at": format_reactivation_time(current),
        }

    if content_invitation_generator is None:
        content_result = _no_op(
            account_id=account_id,
            reason="content_invitation_generator_missing",
            now=current,
        )
    else:
        content_result = content_invitation_generator(
            account_id=account_id,
            now=current,
        )

    invitation = content_result.get("content_invitation")
    if content_result.get("action") == "content_invitation_candidate_created" and isinstance(
        invitation,
        dict,
    ):
        candidate = _candidate_from_content_invitation(
            invitation=invitation,
            now=current,
        )
        state = upsert_reactivation_candidate(
            account_id=account_id,
            candidate=_with_default_schedule(candidate, now=current),
        )
        return {
            "action": "reactivation_candidate_planned",
            "account_id": account_id,
            "reactivation_type": REACTIVATION_TYPE_CONTENT_INVITATION,
            "reactivation_candidate": get_reactivation_candidate_from_metadata(
                state.get("metadata") or {}
            ),
            "topic_followup_generation": topic_result,
            "content_invitation_generation": content_result,
            "evaluated_at": format_reactivation_time(current),
        }

    return {
        "action": "no_op",
        "account_id": account_id,
        "reason": content_result.get("reason") or topic_result.get("reason") or "no_reactivation_candidate",
        "topic_followup_generation": topic_result,
        "content_invitation_generation": content_result,
        "evaluated_at": format_reactivation_time(current),
        "metadata": {},
    }


def dispatch_reactivation_candidate(
    *,
    account_id: str,
    now: Optional[datetime] = None,
    dry_run: bool = True,
    dedupe_checker: Optional[DedupeChecker] = None,
    regenerator: Optional[Regenerator] = None,
) -> Dict[str, Any]:
    """Revalidate and optionally send the current reactivation candidate.

    In dry_run mode this never creates outbound rows and never calls OpenClaw.
    """
    current = now or datetime.now()
    candidate = get_reactivation_candidate(account_id=account_id)
    if candidate is None:
        return _no_op(account_id=account_id, reason="reactivation_candidate_missing", now=current)

    scheduled_at = _parse_reactivation_time(candidate.get("scheduled_at"))
    if scheduled_at is not None and scheduled_at > current:
        return {
            "action": "not_due",
            "account_id": account_id,
            "reactivation_candidate": candidate,
            "scheduled_at": candidate.get("scheduled_at"),
            "evaluated_at": format_reactivation_time(current),
        }

    if _has_sent_reactivation_today(account_id=account_id, now=current):
        clear_reactivation_candidate(
            account_id=account_id,
            reason="reactivation_daily_limit_already_sent",
            now=current,
        )
        return _no_op(
            account_id=account_id,
            reason="reactivation_daily_limit_already_sent",
            now=current,
        )

    recent_inbound = _recent_inbound_count(account_id=account_id, now=current)
    if recent_inbound > 0:
        next_slot = _next_slot_after(candidate, now=current)
        if next_slot:
            state = reschedule_reactivation_candidate(
                account_id=account_id,
                candidate=candidate,
                scheduled_slot=next_slot["scheduled_slot"],
                scheduled_at=next_slot["scheduled_at"],
                reason="recent_inbound",
                now=current,
            )
            return {
                "action": "delayed",
                "account_id": account_id,
                "reason": "recent_inbound",
                "recent_inbound_count": recent_inbound,
                "reactivation_candidate": get_reactivation_candidate_from_metadata(
                    state.get("metadata") or {}
                ),
                "evaluated_at": format_reactivation_time(current),
            }
        clear_reactivation_candidate(
            account_id=account_id,
            reason="recent_inbound_final_slot",
            now=current,
        )
        return _no_op(
            account_id=account_id,
            reason="recent_inbound_final_slot",
            now=current,
            metadata={"recent_inbound_count": recent_inbound},
        )

    avoidance_count = _avoidance_count(account_id=account_id, now=current)
    if avoidance_count > 0:
        next_slot = _next_slot_after(candidate, now=current)
        if next_slot:
            state = reschedule_reactivation_candidate(
                account_id=account_id,
                candidate=candidate,
                scheduled_slot=next_slot["scheduled_slot"],
                scheduled_at=next_slot["scheduled_at"],
                reason="avoidance_window",
                now=current,
            )
            return {
                "action": "delayed",
                "account_id": account_id,
                "reason": "avoidance_window",
                "avoidance_count": avoidance_count,
                "reactivation_candidate": get_reactivation_candidate_from_metadata(
                    state.get("metadata") or {}
                ),
                "evaluated_at": format_reactivation_time(current),
            }
        clear_reactivation_candidate(
            account_id=account_id,
            reason="avoidance_window_final_slot",
            now=current,
        )
        return _no_op(
            account_id=account_id,
            reason="avoidance_window_final_slot",
            now=current,
            metadata={"avoidance_count": avoidance_count},
        )

    latest_message_id = get_latest_message_id_for_account(account_id=account_id)
    cutoff_id = candidate.get("source_message_cutoff_id")
    if (
        latest_message_id is not None
        and cutoff_id is not None
        and int(latest_message_id) > int(cutoff_id)
        and regenerator is not None
    ):
        regenerated = regenerator(account_id=account_id, now=current)
        next_candidate = regenerated.get("reactivation_candidate")
        if isinstance(next_candidate, dict):
            upsert_reactivation_candidate(
                account_id=account_id,
                candidate=_with_default_schedule(next_candidate, now=current),
            )
            candidate = get_reactivation_candidate(account_id=account_id) or candidate
        else:
            clear_reactivation_candidate(
                account_id=account_id,
                reason="regenerate_failed_after_new_message",
                now=current,
            )
            return _no_op(
                account_id=account_id,
                reason="regenerate_failed_after_new_message",
                now=current,
                metadata={"regeneration": regenerated},
            )

    checker = dedupe_checker or llm_reactivation_dedupe_check
    dedupe = checker(account_id=account_id, candidate=candidate, now=current)
    if dedupe.get("duplicate"):
        if regenerator is not None:
            regenerated = regenerator(
                account_id=account_id,
                now=current,
                dedupe_feedback=dedupe,
            )
            next_candidate = regenerated.get("reactivation_candidate")
            if isinstance(next_candidate, dict):
                upsert_reactivation_candidate(
                    account_id=account_id,
                    candidate=_with_default_schedule(next_candidate, now=current),
                )
                candidate = get_reactivation_candidate(account_id=account_id) or candidate
                dedupe = checker(account_id=account_id, candidate=candidate, now=current)
                dedupe["retry_count"] = 1
        if dedupe.get("duplicate"):
            clear_reactivation_candidate(
                account_id=account_id,
                reason="dedupe_duplicate_after_retry",
                now=current,
            )
            return _no_op(
                account_id=account_id,
                reason="dedupe_duplicate_after_retry",
                now=current,
                metadata={"dedupe": dedupe},
            )

    route = _select_route(account_id)
    if route is None:
        return _no_op(account_id=account_id, reason="missing_channel_route", now=current)

    outbound_metadata = reactivation_outbound_metadata(
        candidate={**candidate, "dedupe": dedupe},
        sent_at=current,
        extra={
            "source": "reactivation_scheduler",
            "policy": {
                "daily_limit_key": "reactivation",
                "recent_inbound_delay_minutes": getattr(
                    settings,
                    "reactivation_recent_inbound_delay_minutes",
                    60,
                ),
                "avoidance_applied": False,
            },
            "channel_binding_id": route.get("channel_binding_id"),
            "dry_run": bool(dry_run),
        },
    )
    if dry_run:
        return {
            "action": "would_send",
            "account_id": account_id,
            "text": candidate["text"],
            "reactivation_candidate": candidate,
            "outbound_metadata": outbound_metadata,
            "evaluated_at": format_reactivation_time(current),
        }

    # content_invitation candidates are backed by a content_invitations row that
    # drives the downstream "send titles" interaction. Reactivation is now the
    # single dispatch path, so it owns advancing that row's state machine:
    # candidate -> sending (claim) -> invited (on send) / candidate (on failure).
    is_content_invitation = candidate["type"] == REACTIVATION_TYPE_CONTENT_INVITATION
    invitation_id = (
        _clean_text(candidate.get("content_invitation_id")) if is_content_invitation else ""
    )
    if is_content_invitation:
        if not invitation_id:
            clear_reactivation_candidate(
                account_id=account_id, reason="content_invitation_id_missing", now=current
            )
            return _no_op(
                account_id=account_id, reason="content_invitation_id_missing", now=current
            )
        # Claim ignores scheduled_at; timing was already decided above.
        claimed_invitation = claim_content_invitation_for_send(invitation_id=invitation_id)
        if claimed_invitation is None:
            # Row already sent/expired/claimed elsewhere -> candidate is stale.
            clear_reactivation_candidate(
                account_id=account_id, reason="content_invitation_not_claimable", now=current
            )
            return _no_op(
                account_id=account_id, reason="content_invitation_not_claimable", now=current
            )

    outbound = send_proactive_text(
        account_id=account_id,
        channel=route["channel"],
        channel_account_id=route.get("channel_account_id"),
        to_user_id=route["to_user_id"],
        session_key=route.get("session_key"),
        source="reactivation",
        text=candidate["text"],
        idempotency_key=f"reactivation-{account_id}-{candidate['id']}-{current.date().isoformat()}",
        now=current,
        product_category=_reactivation_product_category(candidate["type"]),
        metadata=outbound_metadata,
    )
    outbound_status = outbound.get("status")
    if is_content_invitation:
        if outbound_status == "sent":
            outbound_id = int(outbound["id"]) if outbound.get("id") is not None else None
            mark_content_invitation_invited(
                invitation_id=invitation_id,
                outbound_message_id=outbound_id,
                invited_at=format_reactivation_time(current),
            )
        else:
            # Send blocked/failed -> reset the row so a later slot can retry.
            release_content_invitation_claim(invitation_id=invitation_id)

    if outbound_status == "sent":
        clear_reactivation_candidate(
            account_id=account_id,
            reason="sent",
            now=current,
        )
    return {
        "action": "sent" if outbound_status == "sent" else "send_blocked",
        "account_id": account_id,
        "reason": outbound.get("error"),
        "outbound_message": outbound,
        "reactivation_candidate": candidate,
        "evaluated_at": format_reactivation_time(current),
    }


def dispatch_due_reactivation_candidates(
    *,
    now: Optional[datetime] = None,
    limit: int = 20,
    dispatch_enabled: Optional[bool] = None,
    dry_run: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """Send any reactivation candidates whose slot time has arrived.

    Runs every scheduler tick, decoupled from the hourly planning pass, so a
    candidate fires at its scheduled slot (plus jitter) rather than waiting for
    the account's next planning. Gated by reactivation_dispatch_enabled; defaults
    fail closed (disabled + dry-run) unless settings/caller opt in.
    """
    current = now or datetime.now()
    if dispatch_enabled is None:
        dispatch_enabled = bool(getattr(settings, "reactivation_dispatch_enabled", False))
    if dry_run is None:
        dry_run = bool(getattr(settings, "reactivation_dispatch_dry_run", True))
    if not dispatch_enabled:
        return []
    due_accounts = list_due_reactivation_candidate_accounts(
        now=format_reactivation_time(current),
        limit=limit,
    )
    results: List[Dict[str, Any]] = []
    for account_id in due_accounts:
        results.append(
            dispatch_reactivation_candidate(
                account_id=account_id,
                now=current,
                dry_run=dry_run,
            )
        )
    return results
