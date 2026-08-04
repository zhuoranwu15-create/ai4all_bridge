"""account_check 候选的决策 + 出站派发（消费已存在的 candidate）。"""
from datetime import datetime
from typing import Any, Dict, Optional

from app.time_utils import beijing_naive_now
from app.db import get_account, get_proactive_account_state
from app.products.zhaoxi.proactive.contract.common import _select_route
from app.products.zhaoxi.proactive.delivery.outbound import dispatch_proactive_text
from app.products.zhaoxi.proactive.delivery.touch_state import STALE, get_account_touch_state
from app.products.zhaoxi.proactive.recall._shared import (
    ACCOUNT_CHECK_CANDIDATE_KEY,
    ACCOUNT_CHECK_SOURCE,
    _candidate_from_state_metadata,
    _format_decision_time,
    _no_op,
    _parse_state_time,
)


def decide_account_check_action(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = now or beijing_naive_now()
    current_text = _format_decision_time(current)

    account = get_account(account_id=account_id)
    if account is None:
        return _no_op(account_id=account_id, reason="account_not_found", now=current)
    if account.get("status") != "active":
        return _no_op(account_id=account_id, reason="account_not_active", now=current)
    state = get_proactive_account_state(account_id=account_id)
    if state is None:
        return _no_op(account_id=account_id, reason="proactive_state_missing", now=current)
    if not state.get("enabled"):
        return _no_op(account_id=account_id, reason="proactive_disabled", now=current)

    cooldown_until = _parse_state_time(state.get("cooldown_until"))
    if cooldown_until is not None and cooldown_until > current:
        return _no_op(
            account_id=account_id,
            reason="cooldown",
            now=current,
            metadata={"cooldown_until": state.get("cooldown_until")},
        )

    quota_date = current.date().isoformat()

    metadata = state.get("metadata") or {}
    candidate = _candidate_from_state_metadata(metadata)
    if candidate is None:
        return _no_op(account_id=account_id, reason="no_candidate", now=current)

    route = _select_route(account_id)
    if route is None:
        return _no_op(
            account_id=account_id,
            reason="missing_channel_route",
            now=current,
            metadata={"candidate_id": candidate["id"]},
        )

    if get_account_touch_state(account_id=account_id, now=current) == STALE:
        return _no_op(
            account_id=account_id, reason="proactive_touch_stale", now=current
        )

    return {
        "action": "send_text",
        "account_id": account_id,
        "source": ACCOUNT_CHECK_SOURCE,
        "text": candidate["text"],
        "idempotency_key": f"account-check-{account_id}-{candidate['id']}-{quota_date}",
        "route": route,
        "candidate": candidate,
        "evaluated_at": current_text,
        "metadata": {
            "quota_date": quota_date,
            "decision": "metadata_candidate",
        },
    }

def execute_account_check_decision(
    *,
    decision: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    if decision.get("action") != "send_text":
        return {
            "status": "skipped",
            "reason": decision.get("reason") or "not_send_text",
            "decision": decision,
        }

    route = decision.get("route") or {}
    # 多机:走 dispatch 而非直接 send,远程账号会 enqueue 由归属节点 pull 发送,
    # 避免中心(或非归属节点)对远程会话本机误发失败(见 dispatch_proactive_text 文档)。
    outbound = dispatch_proactive_text(
        account_id=decision["account_id"],
        channel=route["channel"],
        channel_account_id=route.get("channel_account_id"),
        to_user_id=route["to_user_id"],
        session_key=route.get("session_key"),
        source=decision.get("source") or ACCOUNT_CHECK_SOURCE,
        text=decision["text"],
        idempotency_key=decision.get("idempotency_key"),
        now=now,
        bypass_quiet_hours=False,
        product_category="companion_followup",
        metadata={
            **(decision.get("metadata") or {}),
            ACCOUNT_CHECK_CANDIDATE_KEY: decision.get("candidate") or {},
            "decision_evaluated_at": decision.get("evaluated_at"),
            "channel_binding_id": route.get("channel_binding_id"),
        },
    )
    return {
        "status": outbound.get("status"),
        "reason": outbound.get("error"),
        "decision": decision,
        "outbound_message": outbound,
    }
