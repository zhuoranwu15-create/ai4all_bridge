import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app.config import settings
from app.db import (
    ACCOUNT_ACTIVE_SESSION_KEY,
    close_session,
    get_or_create_session,
    get_session,
    list_active_sessions_for_business_day_before,
)


logger = logging.getLogger("ai4all.session_lifecycle")


def business_day_for(
    now: datetime,
    *,
    start_hour: int = 4,
) -> str:
    """Return the business day string for a datetime and day-boundary hour."""
    if start_hour < 0 or start_hour > 23:
        raise ValueError("start_hour must be between 0 and 23")
    day = now.date()
    if now.hour < start_hour:
        day = day - timedelta(days=1)
    return day.isoformat()


def _close_reason_for(
    session: Dict[str, Any],
    *,
    business_day: Optional[str],
    max_turns: int,
) -> Optional[str]:
    if session.get("status") != "active":
        return "replaced"
    session_business_day = str(session.get("business_day") or "").strip()
    if business_day and session_business_day and session_business_day != business_day:
        return "daily_dreaming"
    if max_turns > 0 and int(session.get("turn_count") or 0) >= max_turns:
        return "max_turns"
    return None


def _fallback_close_summary(
    *,
    account_id: str,
    session_id: int,
    close_reason: str,
    source_business_day: Optional[str],
) -> Dict[str, Any]:
    from app.dreaming import DREAMING_PROMPT_VERSION, run_dreaming

    result = run_dreaming(
        account_id=account_id,
        today=source_business_day or datetime.now().date().isoformat(),
        days=1,
        source_type="max_turns_compression"
        if close_reason == "max_turns"
        else "daily_dreaming",
        source_session_id=session_id,
        source_business_day=source_business_day,
        actor_type="system",
        actor_id="session_lifecycle",
        allow_fallback=True,
    )
    summary = result.get("session_summary") or {}
    return {
        "session_summary": str(summary.get("rough_summary") or ""),
        "carryover_summary": str(summary.get("carryover_summary") or ""),
        "summary_model": "deterministic_fallback"
        if result.get("reason") == "fallback_used"
        else settings.llm_model,
        "summary_prompt_version": DREAMING_PROMPT_VERSION,
        "dreaming_result": result,
    }


def _rotate_session_with_dreaming(
    *,
    session: Dict[str, Any],
    close_reason: str,
) -> Dict[str, Any]:
    account_id = str(session["account_id"])
    session_id = int(session["id"])
    source_business_day = session.get("business_day")
    summary = _fallback_close_summary(
        account_id=account_id,
        session_id=session_id,
        close_reason=close_reason,
        source_business_day=source_business_day,
    )
    archived_session_key = f"{ACCOUNT_ACTIVE_SESSION_KEY}:{session_id}"
    close_session(
        session_id=session_id,
        close_reason=close_reason,
        archived_session_key=archived_session_key,
        session_summary=summary.get("session_summary"),
        carryover_summary=summary.get("carryover_summary"),
        summary_model=summary.get("summary_model"),
        summary_prompt_version=summary.get("summary_prompt_version"),
    )
    return summary


def get_or_create_account_active_session_with_dreaming(
    *,
    account_id: str,
    channel: str,
    sender_id: str,
    sender_name: Optional[str],
    chat_id: Optional[str],
    business_day: Optional[str] = None,
    max_turns: Optional[int] = None,
) -> Dict[str, Any]:
    """Return active session, rotating with LLM Dreaming before creating a new one."""
    max_turns_value = max(0, int(max_turns or 0))
    initial = get_or_create_session(
        account_id=account_id,
        channel=channel,
        sender_id=sender_id,
        sender_name=sender_name,
        chat_id=chat_id,
        session_key=ACCOUNT_ACTIVE_SESSION_KEY,
        business_day=business_day,
    )
    session = initial["session"]
    close_reason = _close_reason_for(
        session,
        business_day=business_day,
        max_turns=max_turns_value,
    )
    if not close_reason:
        return initial

    summary = _rotate_session_with_dreaming(
        session=session,
        close_reason=close_reason,
    )
    next_state = get_or_create_session(
        account_id=account_id,
        channel=channel,
        sender_id=sender_id,
        sender_name=sender_name,
        chat_id=chat_id,
        session_key=ACCOUNT_ACTIVE_SESSION_KEY,
        business_day=business_day,
        carryover_summary=summary.get("carryover_summary"),
        metadata={"created_reason": close_reason},
    )
    if summary.get("dreaming_result") is not None:
        next_state["dreaming_result"] = summary["dreaming_result"]
    return next_state


def run_daily_dreaming_scan(
    *,
    now: Optional[datetime] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """Scan active sessions from previous business days and rotate them."""
    current = now or datetime.now()
    current_business_day = business_day_for(
        current,
        start_hour=int(getattr(settings, "conversation_session_business_day_start_hour", 4)),
    )
    sessions = list_active_sessions_for_business_day_before(
        business_day=current_business_day,
        limit=limit,
    )
    results = []
    for session in sessions:
        try:
            _rotate_session_with_dreaming(
                session=session,
                close_reason="daily_dreaming",
            )
            refreshed = get_session(session_id=int(session["id"]))
            results.append(
                {
                    "status": "rotated",
                    "account_id": session["account_id"],
                    "session_id": session["id"],
                    "business_day": session.get("business_day"),
                    "close_reason": refreshed.get("close_reason") if refreshed else "daily_dreaming",
                }
            )
        except Exception as exc:
            logger.exception(
                "daily dreaming scan failed account=%s session=%s error=%s",
                session.get("account_id"),
                session.get("id"),
                exc,
            )
            results.append(
                {
                    "status": "failed",
                    "account_id": session.get("account_id"),
                    "session_id": session.get("id"),
                    "error": str(exc),
                }
            )
    return {
        "status": "ok",
        "business_day": current_business_day,
        "scanned": len(sessions),
        "results": results,
    }
