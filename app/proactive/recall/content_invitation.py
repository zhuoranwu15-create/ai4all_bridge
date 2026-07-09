"""content_invitation 内容邀请候选生成（内容型话题，走工具 + 可选 web search）。"""
import json
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app.config import settings
from app.time_utils import beijing_naive_now
from app.db import (
    get_account,
    get_active_content_invitation,
    get_pending_companion_followup_count_in_window,
    get_pending_reminder_count_in_window,
    get_proactive_account_state,
    list_content_invitations_for_account,
    list_sessions_for_account,
    upsert_proactive_account_state,
)
from app.llm import generate_reply_with_tools, is_llm_configured
from app.proactive.contract.common import _clean_text, _select_route, _truncate_text
from app.proactive.delivery.touch_state import STALE, get_account_touch_state
from app.user_profiles import read_agent_context
from app.tools import get_content_invitation_generation_tools, get_web_search_tools
from app.turn_context import TurnContext
from app.proactive.contract.prompts import CONTENT_INVITATION_SYSTEM_PROMPT
from app.proactive.recall._shared import _format_decision_time, _latest_session_history, _no_op


def _build_content_invitation_user_prompt(
    *,
    account: Dict[str, Any],
    state: Dict[str, Any],
    history: list[dict[str, str]],
    now: datetime,
    existing_counts: Dict[str, Any],
) -> str:
    account_id = str(account["id"])
    agent_context = read_agent_context(
        account_id,
        display_name=account.get("display_name"),
    )
    context_blocks = agent_context.blocks
    history_lines = [
        f"- {item['role']}: {_truncate_text(item.get('content') or '', 300)}"
        for item in history
        if _clean_text(item.get("content"))
    ]
    return "\n\n".join(
        [
            f"now: {_format_decision_time(now)}",
            f"account_id: {account_id}",
            "USER.md:\n" + _truncate_text(context_blocks.get("USER", ""), 1200),
            "MEMORY.md:\n" + _truncate_text(context_blocks.get("MEMORY", ""), 1600),
            "proactive_state_metadata:\n"
            + _truncate_text(json.dumps(state.get("metadata") or {}, ensure_ascii=False), 1200),
            "existing_proactive_counts:\n"
            + _truncate_text(json.dumps(existing_counts, ensure_ascii=False), 1200),
            "recent_chat:\n" + ("\n".join(history_lines) if history_lines else "- none"),
        ]
    )

def generate_content_invitation_candidate(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Run an account proactive check pass that may create one content invitation candidate."""
    current = now or beijing_naive_now()
    current_text = _format_decision_time(current)

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

    route = _select_route(account_id)
    if route is None:
        return _no_op(account_id=account_id, reason="missing_channel_route", now=current)

    active_invitation = get_active_content_invitation(
        account_id=account_id,
        now=current_text,
    )
    if active_invitation is not None:
        return _no_op(
            account_id=account_id,
            reason="active_content_invitation_exists",
            now=current,
            metadata={"content_invitation_id": active_invitation["id"]},
        )

    pending_candidates = list_content_invitations_for_account(
        account_id=account_id,
        status="candidate",
        limit=1,
    )
    if pending_candidates:
        return _no_op(
            account_id=account_id,
            reason="content_invitation_candidate_exists",
            now=current,
            metadata={"content_invitation_id": pending_candidates[0]["id"]},
        )

    try:
        avoidance_hours = int(getattr(settings, "proactive_avoidance_window_hours", 6) or 0)
    except (TypeError, ValueError):
        avoidance_hours = 6
    existing_counts: Dict[str, Any] = {}
    if avoidance_hours > 0:
        window_end = current + timedelta(hours=avoidance_hours)
        window_start_text = current_text
        window_end_text = _format_decision_time(window_end)
        reminder_count = get_pending_reminder_count_in_window(
            account_id=account_id,
            start_at=window_start_text,
            end_at=window_end_text,
        )
        existing_counts["avoidance_user_reminder_count"] = reminder_count
        if reminder_count > 0:
            return _no_op(
                account_id=account_id,
                reason="avoidance_window_user_reminder",
                now=current,
                metadata=existing_counts,
            )
        companion_count = get_pending_companion_followup_count_in_window(
            account_id=account_id,
            start_at=window_start_text,
            end_at=window_end_text,
        )
        existing_counts["avoidance_companion_followup_count"] = companion_count
        if companion_count > 0:
            return _no_op(
                account_id=account_id,
                reason="avoidance_window_companion_followup",
                now=current,
                metadata=existing_counts,
            )

    sessions = list_sessions_for_account(account_id=account_id, limit=1)
    if not sessions:
        return _no_op(account_id=account_id, reason="session_missing", now=current)
    session = sessions[0]
    history = _latest_session_history(
        account_id=account_id,
        limit=int(getattr(settings, "proactive_account_check_context_messages", 12) or 12),
    )
    if not history:
        return _no_op(account_id=account_id, reason="no_recent_history", now=current)

    before_ids = {
        item["id"]
        for item in list_content_invitations_for_account(
            account_id=account_id,
            limit=20,
        )
    }
    tools = get_content_invitation_generation_tools()
    web_search_enabled = bool(getattr(settings, "web_search_enabled", False))
    if web_search_enabled:
        tools = [*get_web_search_tools(), *tools]
    ctx = TurnContext(
        account_id=account_id,
        account=account,
        session=session,
        identity=None,
        binding=route,
        message_id=f"account-check-content-{account_id}-{current.strftime('%Y%m%d%H%M%S')}",
        text="",
        today=current.date().isoformat(),
        business_day=current.date().isoformat(),
        profile_path=None,
        debug_trace_enabled=False,
        onboarding_state="complete",
        onboarding_active=False,
        recent_messages=history,
        background_loop=None,
        web_search_enabled=web_search_enabled,
    )
    system_prompt = CONTENT_INVITATION_SYSTEM_PROMPT
    user_prompt = _build_content_invitation_user_prompt(
        account=account,
        state=state,
        history=history,
        now=current,
        existing_counts=existing_counts,
    )
    reply, error = generate_reply_with_tools(
        user_text="content_invitation_generation",
        history=[{"role": "user", "content": user_prompt}],
        system_prompt=system_prompt,
        tools=tools,
        ctx=ctx,
        max_tool_rounds=int(
            getattr(settings, "proactive_content_invitation_tool_rounds", 5) or 5
        ),
    )
    if error:
        return _no_op(
            account_id=account_id,
            reason="content_invitation_generation_failed",
            now=current,
            metadata={"error": error},
        )

    after = list_content_invitations_for_account(account_id=account_id, limit=20)
    created = next((item for item in after if item["id"] not in before_ids), None)
    if created is None:
        upsert_proactive_account_state(
            account_id=account_id,
            metadata_patch={
                "content_invitation_generation_checked_at": current_text,
                "content_invitation_generation_last_reply": _truncate_text(reply or "", 400),
            },
        )
        return _no_op(
            account_id=account_id,
            reason="llm_no_content_invitation",
            now=current,
            metadata={"reply": _truncate_text(reply or "", 400)},
        )

    next_state = upsert_proactive_account_state(
        account_id=account_id,
        metadata_patch={
            "content_invitation_generation_checked_at": current_text,
            "content_invitation_candidate_created_at": current_text,
            "content_invitation_candidate_id": created["id"],
        },
    )
    return {
        "action": "content_invitation_candidate_created",
        "account_id": account_id,
        "content_invitation": created,
        "evaluated_at": current_text,
        "proactive_state": next_state,
    }
