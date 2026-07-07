import logging
from datetime import datetime, timedelta

from app.time_utils import beijing_now
from typing import Any, Dict, Optional

from app.config import settings
from app.db import (
    ACCOUNT_ACTIVE_SESSION_KEY,
    close_session,
    get_latest_closed_carryover_for_account,
    get_or_create_session,
    get_session,
    list_active_sessions_for_business_day_before,
    update_session_rolling_summary,
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
) -> Optional[str]:
    if session.get("status") != "active":
        return "replaced"
    session_business_day = str(session.get("business_day") or "").strip()
    if business_day and session_business_day and session_business_day != business_day:
        return "daily_dreaming"
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
        today=source_business_day or beijing_now().date().isoformat(),
        days=1,
        source_type="daily_dreaming",
        source_session_id=session_id,
        source_business_day=source_business_day,
        actor_type="system",
        actor_id="session_lifecycle",
        allow_fallback=True,
    )
    summary = result.get("session_summary") or {}
    from app.llm import get_active_llm_model

    return {
        # rough_summary 停产后，session_summary 统一取 carryover（单一摘要）。
        "session_summary": str(summary.get("carryover_summary") or ""),
        "carryover_summary": str(summary.get("carryover_summary") or ""),
        "summary_model": "deterministic_fallback"
        if result.get("reason") == "fallback_used"
        else get_active_llm_model(),
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
) -> Dict[str, Any]:
    """Return active session, rotating with LLM Dreaming before creating a new one."""
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
    )
    if not close_reason:
        # 非本次轮转。但若这是 scheduler 关闭旧 session 后懒创建的空 active session，补种上一段
        # carryover（否则 scheduler 路径会丢失前一天的延续，见 _maybe_seed_new_session_from_last_closed）。
        _maybe_seed_new_session_from_last_closed(session)
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
    _maybe_seed_new_session_from_last_closed(next_state["session"])
    if summary.get("dreaming_result") is not None:
        next_state["dreaming_result"] = summary["dreaming_result"]
    return next_state


def _maybe_seed_new_session_from_last_closed(session: Dict[str, Any]) -> None:
    """为「刚创建、尚无消息、尚无 rolling」的新 active session 补种上一段 carryover 作为 rolling seed。

    统一的 seed 入口：无论新 session 是懒轮转刚建、还是 4 点 scheduler 关闭旧 session 后由下条消息
    懒建，carryover 都已落在被关闭的 session 行上（`close_session(carryover_summary=…)`），故一律从
    最近一个已关闭 session 回填。守卫「无 rolling 且 turn_count==0」确保只在新 session 首条消息时补种、
    绝不污染进行中的会话（首条后 turn_count>0 即短路，不再查库）。upto_id=0：新 session 尚无消息，seed
    不对应任何消息 id；此后会话内溢出从 0 起继续 merge。回写 session dict 使本轮组装立即看到 seed。
    """
    if (session.get("rolling_summary") or "").strip():
        return
    if int(session.get("turn_count") or 0) > 0:
        return
    seed = (get_latest_closed_carryover_for_account(account_id=str(session["account_id"])) or "").strip()
    if not seed:
        return
    update_session_rolling_summary(
        session_id=int(session["id"]),
        rolling_summary=seed,
        rolling_summary_upto_id=0,
    )
    session["rolling_summary"] = seed
    session["rolling_summary_upto_id"] = 0


def run_daily_dreaming_scan(
    *,
    now: Optional[datetime] = None,
    limit: int = 100,
    node_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Scan active sessions from previous business days and rotate them.

    node_id 非空时只处理归属该节点的账号（厚节点改造 P4 调度分片）。
    """
    current = now or beijing_now()
    current_business_day = business_day_for(
        current,
        start_hour=int(getattr(settings, "conversation_session_business_day_start_hour", 4)),
    )
    sessions = list_active_sessions_for_business_day_before(
        business_day=current_business_day,
        limit=limit,
        node_id=node_id,
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
