"""topic_followup 拉活候选生成（个人续聊话题，归入 companion_followup）。"""
import json
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app.config import settings
from app.time_utils import beijing_naive_now
from app.db import (
    get_account,
    get_proactive_account_state,
    list_recent_messages_for_account_since,
    list_recent_reactivation_outbound_messages,
)
from app.llm import generate_completion, is_llm_configured
from app.proactive.contract.common import _clean_text, _extract_json_object, _select_route, _truncate_text
from app.proactive.delivery.touch_state import STALE, get_account_touch_state
from app.user_profiles import read_agent_context
from app.proactive.store.candidates import REACTIVATION_TYPE_TOPIC_FOLLOWUP
from app.proactive.contract.prompts import TOPIC_FOLLOWUP_SYSTEM_PROMPT
from app.proactive.recall._shared import _format_decision_time, _no_op


def _build_topic_followup_user_prompt(
    *,
    account: Dict[str, Any],
    state: Dict[str, Any],
    history: list[dict[str, Any]],
    now: datetime,
    recent_sent_topics: Optional[list] = None,
) -> str:
    account_id = str(account["id"])
    agent_context = read_agent_context(
        account_id,
        display_name=account.get("display_name"),
    )
    context_blocks = agent_context.blocks
    history_lines = [
        (
            f"- #{item.get('id')} {item.get('created_at')} "
            f"{item['role']}: {_truncate_text(item.get('content') or '', 300)}"
        )
        for item in history
        if _clean_text(item.get("content"))
    ]
    sent_lines = (
        "\n".join(
            f"- {item.get('sent_at') or item.get('created_at')} topic={item.get('topic')} text={_truncate_text(item.get('text') or '', 80)}"
            for item in (recent_sent_topics or [])
        )
        or "- none"
    )
    return "\n\n".join(
        [
            f"now: {_format_decision_time(now)}",
            f"account_id: {account_id}",
            "SOUL.md:\n" + _truncate_text(context_blocks.get("SOUL", ""), 1000),
            "USER.md:\n" + _truncate_text(context_blocks.get("USER", ""), 1200),
            "proactive_state_metadata:\n"
            + _truncate_text(json.dumps(state.get("metadata") or {}, ensure_ascii=False), 1200),
            "recent_sent_reactivations:\n" + sent_lines,
            "recent_72h_chat:\n" + ("\n".join(history_lines) if history_lines else "- none"),
        ]
    )

def _normalize_topic_followup_candidate(
    payload: Dict[str, Any],
    *,
    now: datetime,
    source_message_cutoff_id: Optional[int],
) -> Optional[Dict[str, Any]]:
    if payload.get("should_send") is not True:
        return None
    text = _clean_text(payload.get("text"))
    if not text:
        return None
    confidence = float(payload.get("confidence") or 0)
    min_confidence = float(
        getattr(settings, "proactive_account_check_min_confidence", 0.85)
        or 0.85
    )
    if confidence < min_confidence:
        return None
    # Include microseconds so a regenerate triggered within the same wall-clock
    # second produces a different candidate id and a different outbound
    # idempotency_key (reactivation-{account}-{id}-{date}).
    candidate: Dict[str, Any] = {
        "id": "reactivation-topic-" + now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond:06d}",
        "type": REACTIVATION_TYPE_TOPIC_FOLLOWUP,
        "text": text[:120],
        "topic": _clean_text(payload.get("topic")) or "topic_followup",
        "reason": _clean_text(payload.get("reason")) or "topic_followup_llm_candidate",
        "confidence": confidence,
        "generated_at": _format_decision_time(now),
    }
    if source_message_cutoff_id is not None:
        candidate["source_message_cutoff_id"] = source_message_cutoff_id
    return candidate

def generate_topic_followup_candidate(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Generate one topic_followup reactivation candidate from recent account chat."""
    current = now or beijing_naive_now()

    account = get_account(account_id=account_id)
    if account is None:
        return _no_op(account_id=account_id, reason="account_not_found", now=current)
    if account.get("status") != "active":
        return _no_op(account_id=account_id, reason="account_not_active", now=current)
    if get_account_touch_state(account_id=account_id, now=current) == STALE:
        return _no_op(account_id=account_id, reason="proactive_touch_stale", now=current)

    state = get_proactive_account_state(account_id=account_id)
    if state is None:
        return _no_op(account_id=account_id, reason="proactive_state_missing", now=current)
    if not state.get("enabled"):
        return _no_op(account_id=account_id, reason="proactive_disabled", now=current)

    if not is_llm_configured():
        return _no_op(account_id=account_id, reason="llm_disabled", now=current)

    if _select_route(account_id) is None:
        return _no_op(account_id=account_id, reason="missing_channel_route", now=current)

    try:
        window_hours = int(getattr(settings, "reactivation_topic_followup_window_hours", 72) or 72)
    except (TypeError, ValueError):
        window_hours = 72
    try:
        context_limit = int(getattr(settings, "reactivation_topic_followup_context_messages", 100) or 100)
    except (TypeError, ValueError):
        context_limit = 100

    # messages.created_at 存北京时间（insert 时 datetime('now','+8 hours')），current 也是
    # 北京 naive 时间，直接按北京时间比较，**不做 UTC 转换**（旧 local_to_utc_string 会把
    # 阈值偏移一个时区，导致窗口错位）。
    since_local = _format_decision_time(current - timedelta(hours=max(window_hours, 1)))
    history = list_recent_messages_for_account_since(
        account_id=account_id,
        since=since_local,
        limit=max(1, context_limit),
    )
    if not history:
        return _no_op(
            account_id=account_id,
            reason="no_recent_72h_history",
            now=current,
            metadata={"since": since_local},
        )

    try:
        dedupe_days = int(getattr(settings, "reactivation_dedupe_days", 3) or 3)
    except (TypeError, ValueError):
        dedupe_days = 3
    # outbound_messages.created_at 同为北京时间，直接按北京时间比较，不做 UTC 转换。
    since_dedupe = _format_decision_time(current - timedelta(days=max(dedupe_days, 1)))
    sent_history = list_recent_reactivation_outbound_messages(
        account_id=account_id,
        since=since_dedupe,
        limit=20,
    )
    recent_sent_topics = [
        {
            "sent_at": item.get("sent_at") or item.get("created_at"),
            "topic": (item.get("metadata") or {}).get("topic"),
            "text": item.get("text"),
        }
        for item in sent_history
        if (item.get("metadata") or {}).get("topic")
    ]

    messages = [
        {"role": "system", "content": TOPIC_FOLLOWUP_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _build_topic_followup_user_prompt(
                account=account,
                state=state,
                history=history,
                now=current,
                recent_sent_topics=recent_sent_topics,
            ),
        },
    ]
    try:
        raw = generate_completion(messages)
        payload = _extract_json_object(raw)
        source_cutoff = max((int(item["id"]) for item in history if item.get("id") is not None), default=None)
        candidate = _normalize_topic_followup_candidate(
            payload,
            now=current,
            source_message_cutoff_id=source_cutoff,
        )
    except Exception as err:
        return _no_op(
            account_id=account_id,
            reason="topic_followup_generation_failed",
            now=current,
            metadata={"error": str(err)},
        )

    if candidate is None:
        return _no_op(
            account_id=account_id,
            reason="llm_no_topic_followup_candidate",
            now=current,
            metadata={"reply": _truncate_text(raw or "", 400)},
        )

    return {
        "action": "topic_followup_candidate_created",
        "account_id": account_id,
        "reactivation_candidate": candidate,
        "evaluated_at": _format_decision_time(current),
    }
