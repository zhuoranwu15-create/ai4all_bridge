"""朝夕账号会话轮转与 Dreaming 衔接。"""

import logging
from datetime import datetime, timedelta

from app.time_utils import beijing_now
from typing import TYPE_CHECKING, Any, Dict, Optional

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

if TYPE_CHECKING:
    from app.agent_runtime.ports import MemorySink


logger = logging.getLogger("ai4all.session_lifecycle")
_default_memory_sink: Optional["MemorySink"] = None


def configure_memory_sink(memory_sink: Optional["MemorySink"]) -> None:
    """配置懒轮转使用的进程级默认 typed sink；传 None 清除。"""
    global _default_memory_sink
    _default_memory_sink = memory_sink


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
    memory_sink: Optional["MemorySink"],
) -> Dict[str, Any]:
    from app.products.zhaoxi.application.memory.dreaming import DREAMING_PROMPT_VERSION, run_dreaming

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
        memory_sink=memory_sink,
    )
    summary = result.get("session_summary") or {}
    from app.agent_runtime.llm.service import get_active_llm_model

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
    memory_sink: Optional["MemorySink"] = None,
) -> Dict[str, Any]:
    account_id = str(session["account_id"])
    session_id = int(session["id"])
    source_business_day = session.get("business_day")
    effective_memory_sink = (
        memory_sink if memory_sink is not None else _default_memory_sink
    )
    summary = _fallback_close_summary(
        account_id=account_id,
        session_id=session_id,
        close_reason=close_reason,
        source_business_day=source_business_day,
        memory_sink=effective_memory_sink,
    )
    # 按 session **自身** 的 active key 归档（§7.1）：被轮转的 session 一定是某个 scope
    # 的 active session，其 session_key 即该 scope 的 active key（微信 __account_active__
    # / Web __web_active__ / App __app_active__）。据此归档使各 scope 的 closed 段都带自身
    # 前缀、互不串。回落到微信常量仅为兼容极端缺字段的情况（正常路径 session_key 必有值）。
    active_key = str(session.get("session_key") or ACCOUNT_ACTIVE_SESSION_KEY)
    archived_session_key = f"{active_key}:{session_id}"
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
    active_session_key: str = ACCOUNT_ACTIVE_SESSION_KEY,
    update_account_channel: bool = True,
    memory_sink: Optional["MemorySink"] = None,
) -> Dict[str, Any]:
    """Return active session, rotating with LLM Dreaming before creating a new one.

    ``active_session_key`` 选定 conversation_scope（§7.1）：微信默认
    ``__account_active__``（行为不变），Web/App 传各自 active key，各渠道独立对话线。

    ``update_account_channel=False``（§3）：本次不改写已存在账号的 ``accounts.channel``
    （供 Web 首触已绑微信账号时保留原渠道）。默认 True，微信路径行为不变。
    """
    initial = get_or_create_session(
        account_id=account_id,
        channel=channel,
        sender_id=sender_id,
        sender_name=sender_name,
        chat_id=chat_id,
        session_key=active_session_key,
        business_day=business_day,
        update_account_channel=update_account_channel,
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
        memory_sink=memory_sink,
    )
    next_state = get_or_create_session(
        account_id=account_id,
        channel=channel,
        sender_id=sender_id,
        sender_name=sender_name,
        chat_id=chat_id,
        session_key=active_session_key,
        business_day=business_day,
        carryover_summary=summary.get("carryover_summary"),
        metadata={"created_reason": close_reason},
        update_account_channel=update_account_channel,
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
    # 只承接**同 scope** 的上一段 carryover（§7.2 Model B）：新 active session 的
    # session_key 即其 scope 的 active key（微信 / Web / App 各自的 active key），
    # 据此过滤归档段前缀，避免跨渠道 carryover 泄漏。微信单渠道下与不过滤等价现状。
    active_key = str(session.get("session_key") or ACCOUNT_ACTIVE_SESSION_KEY)
    seed = (
        get_latest_closed_carryover_for_account(
            account_id=str(session["account_id"]),
            active_session_key=active_key,
        )
        or ""
    ).strip()
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
    memory_sink: Optional["MemorySink"] = None,
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
                memory_sink=memory_sink,
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
