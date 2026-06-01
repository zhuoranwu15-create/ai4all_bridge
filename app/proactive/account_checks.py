import json
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app.config import settings
from app.db import (
    get_account,
    get_active_content_invitation,
    get_pending_companion_followup_count_in_window,
    get_pending_reminder_count_in_window,
    get_proactive_account_state,
    list_content_invitations_for_account,
    list_recent_messages,
    list_channel_bindings_for_account,
    list_sessions_for_account,
    upsert_proactive_account_state,
)
from app.llm import generate_completion, generate_reply_with_tools
from app.proactive.messaging import send_proactive_text
from app.tools import get_content_invitation_generation_tools, get_web_search_tools
from app.turn_context import TurnContext
from app.user_profiles import read_agent_context


ACCOUNT_CHECK_SOURCE = "account_check"
ACCOUNT_CHECK_CANDIDATE_KEY = "account_check_candidate"
ACCOUNT_CHECK_CANDIDATE_DRAFT_KEY = "account_check_candidate_draft"
LEGACY_HEARTBEAT_CANDIDATE_KEY = "heartbeat_candidate"
LEGACY_HEARTBEAT_CANDIDATE_DRAFT_KEY = "heartbeat_candidate_draft"


ACCOUNT_CHECK_CANDIDATE_SYSTEM_PROMPT = """你是 AI4ALL 的隐藏账号主动检查候选生成器。

你的任务是判断是否存在一个非常明确、低打扰、高价值的主动关怀候选。

严格规则：
- 只输出 JSON，不输出解释，不输出 Markdown。
- 默认不主动打扰用户；没有强理由时 should_send=false。
- 不要编造事实、日期、承诺或用户目标。
- 只基于输入中的最近对话、记忆、用户偏好生成候选。
- 不做医疗、法律、金融等高风险建议。
- 不提醒普通寒暄、无明确后续价值的内容。
- 输出 text 必须短、自然、像微信消息，最多 80 个中文字符。

JSON schema:
{
  "should_send": false,
  "text": "",
  "reason": "简短原因",
  "confidence": 0.0
}
"""


CONTENT_INVITATION_SYSTEM_PROMPT = """你是 AI4ALL 的隐藏内容邀请候选生成器。

你的任务是判断当前账号是否存在一个非常明确、低打扰、适合朋友式询问的内容邀请候选。

严格规则：
- 必须通过工具完成动作：适合时调用 create_content_invitation_candidate，不适合时调用 skip_content_invitation。
- 不使用关键词触发，不要因为用户偶然提到“新闻/日报/看看”就创建候选。
- 只能基于用户显式关注点、长期画像、近期稳定话题或用户主动请求。
- 主动邀请文本只能是短的询问句，不能包含标题、URL、来源链接、长摘要或日报式表达。
- title_items 至少 3 条，最多 10 条；发给用户前只会展示 title。
- 不要创建医疗、法律、金融投资等高风险内容邀请。
- 不要仅因为当前时间较晚或处于 quiet hours 而跳过；发送时机、quiet hours 和日上限由后端策略处理。
- 没有足够把握时调用 skip_content_invitation。
"""


def _format_decision_time(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _parse_state_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


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
        "evaluated_at": _format_decision_time(now),
        "metadata": metadata or {},
    }


def _select_route(account_id: str) -> Optional[Dict[str, Any]]:
    for binding in list_channel_bindings_for_account(account_id=account_id):
        to_user_id = _clean_text(binding.get("chat_id"))
        channel_account_id = _clean_text(binding.get("channel_account_id"))
        if not to_user_id or not channel_account_id:
            continue
        return {
            "channel_binding_id": binding["id"],
            "channel": binding["channel"],
            "channel_account_id": channel_account_id,
            "to_user_id": to_user_id,
            "session_key": binding.get("session_key"),
        }
    return None


def _candidate_from_state_metadata(
    metadata: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    candidate = metadata.get(ACCOUNT_CHECK_CANDIDATE_KEY)
    if not isinstance(candidate, dict):
        candidate = metadata.get(LEGACY_HEARTBEAT_CANDIDATE_KEY)
    if isinstance(candidate, dict):
        text = _clean_text(candidate.get("text"))
        if text:
            return {
                "id": _clean_text(candidate.get("id")) or "metadata",
                "text": text,
                "source": _clean_text(candidate.get("source")) or "metadata",
                "reason": _clean_text(candidate.get("reason")) or "manual_candidate",
            }

    text = _clean_text(
        metadata.get("account_check_candidate_text")
        or metadata.get("heartbeat_candidate_text")
    )
    if text:
        return {
            "id": _clean_text(
                metadata.get("account_check_candidate_id")
                or metadata.get("heartbeat_candidate_id")
            )
            or "metadata",
            "text": text,
            "source": _clean_text(
                metadata.get("account_check_candidate_source")
                or metadata.get("heartbeat_candidate_source")
            )
            or "metadata",
            "reason": _clean_text(
                metadata.get("account_check_candidate_reason")
                or metadata.get("heartbeat_candidate_reason")
            )
            or "manual_candidate",
        }
    return None


def _latest_session_history(
    *,
    account_id: str,
    limit: int,
) -> list[dict[str, str]]:
    sessions = list_sessions_for_account(account_id=account_id, limit=1)
    if not sessions:
        return []
    return list_recent_messages(
        session_id=int(sessions[0]["id"]),
        limit=max(1, limit),
    )


def _build_candidate_user_prompt(
    *,
    account: Dict[str, Any],
    state: Dict[str, Any],
    history: list[dict[str, str]],
    now: datetime,
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
            "recent_chat:\n" + ("\n".join(history_lines) if history_lines else "- none"),
        ]
    )


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


def _normalize_llm_candidate(
    payload: Dict[str, Any],
    *,
    now: datetime,
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
    return {
        "id": "llm-" + now.strftime("%Y%m%d%H%M%S"),
        "text": text[:120],
        "source": "account_check_llm_candidate_v1",
        "reason": _clean_text(payload.get("reason")) or "llm_candidate",
        "confidence": confidence,
    }


def decide_account_check_action(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = now or datetime.now()
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
    outbound = send_proactive_text(
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


def generate_account_check_candidate_draft(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = now or datetime.now()

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

    if not getattr(settings, "llm_api_key", ""):
        return _no_op(account_id=account_id, reason="llm_disabled", now=current)

    if _select_route(account_id) is None:
        return _no_op(account_id=account_id, reason="missing_channel_route", now=current)

    history = _latest_session_history(
        account_id=account_id,
        limit=int(getattr(settings, "proactive_account_check_context_messages", 12) or 12),
    )
    if not history:
        return _no_op(account_id=account_id, reason="no_recent_history", now=current)

    messages = [
        {"role": "system", "content": ACCOUNT_CHECK_CANDIDATE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _build_candidate_user_prompt(
                account=account,
                state=state,
                history=history,
                now=current,
            ),
        },
    ]
    try:
        raw = generate_completion(messages)
        payload = _extract_json_object(raw)
        candidate = _normalize_llm_candidate(payload, now=current)
    except Exception as err:
        return _no_op(
            account_id=account_id,
            reason="candidate_generation_failed",
            now=current,
            metadata={"error": str(err)},
        )

    next_metadata = dict(state.get("metadata") or {})
    next_metadata["account_check_candidate_draft_generated_at"] = _format_decision_time(current)
    if candidate is None:
        next_metadata.pop(ACCOUNT_CHECK_CANDIDATE_DRAFT_KEY, None)
        next_metadata.pop(LEGACY_HEARTBEAT_CANDIDATE_DRAFT_KEY, None)
        upsert_proactive_account_state(
            account_id=account_id,
            metadata=next_metadata,
        )
        return _no_op(account_id=account_id, reason="llm_no_candidate", now=current)

    next_metadata[ACCOUNT_CHECK_CANDIDATE_DRAFT_KEY] = candidate
    next_metadata.pop(LEGACY_HEARTBEAT_CANDIDATE_DRAFT_KEY, None)
    next_state = upsert_proactive_account_state(
        account_id=account_id,
        metadata=next_metadata,
    )
    return {
        "action": "draft_candidate",
        "account_id": account_id,
        "candidate": candidate,
        "evaluated_at": _format_decision_time(current),
        "proactive_state": next_state,
    }


def generate_content_invitation_candidate(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Run an account proactive check pass that may create one content invitation candidate."""
    current = now or datetime.now()
    current_text = _format_decision_time(current)

    if not getattr(settings, "proactive_content_invitation_generation_enabled", False):
        return _no_op(
            account_id=account_id,
            reason="content_invitation_generation_disabled",
            now=current,
        )

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

    if not getattr(settings, "llm_api_key", ""):
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
    next_metadata = dict(state.get("metadata") or {})
    next_metadata["content_invitation_generation_checked_at"] = current_text
    if created is None:
        next_metadata["content_invitation_generation_last_reply"] = _truncate_text(reply or "", 400)
        upsert_proactive_account_state(
            account_id=account_id,
            metadata=next_metadata,
        )
        return _no_op(
            account_id=account_id,
            reason="llm_no_content_invitation",
            now=current,
            metadata={"reply": _truncate_text(reply or "", 400)},
        )

    next_metadata["content_invitation_candidate_created_at"] = current_text
    next_metadata["content_invitation_candidate_id"] = created["id"]
    next_state = upsert_proactive_account_state(
        account_id=account_id,
        metadata=next_metadata,
    )
    return {
        "action": "content_invitation_candidate_created",
        "account_id": account_id,
        "content_invitation": created,
        "evaluated_at": current_text,
        "proactive_state": next_state,
    }


def promote_account_check_candidate_draft(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = now or datetime.now()
    state = get_proactive_account_state(account_id=account_id)
    if state is None:
        return _no_op(account_id=account_id, reason="proactive_state_missing", now=current)

    metadata = dict(state.get("metadata") or {})
    draft = metadata.get(ACCOUNT_CHECK_CANDIDATE_DRAFT_KEY)
    if not isinstance(draft, dict):
        draft = metadata.get(LEGACY_HEARTBEAT_CANDIDATE_DRAFT_KEY)
    if not isinstance(draft, dict) or not _clean_text(draft.get("text")):
        return _no_op(account_id=account_id, reason="draft_missing", now=current)

    candidate = {
        "id": _clean_text(draft.get("id")) or "draft",
        "text": _clean_text(draft.get("text")),
        "source": _clean_text(draft.get("source")) or "account_check_draft",
        "reason": _clean_text(draft.get("reason")) or "promoted_draft",
    }
    if draft.get("confidence") is not None:
        candidate["confidence"] = draft.get("confidence")

    metadata[ACCOUNT_CHECK_CANDIDATE_KEY] = candidate
    metadata["account_check_candidate_promoted_at"] = _format_decision_time(current)
    metadata.pop(LEGACY_HEARTBEAT_CANDIDATE_KEY, None)
    metadata.pop(ACCOUNT_CHECK_CANDIDATE_DRAFT_KEY, None)
    metadata.pop(LEGACY_HEARTBEAT_CANDIDATE_DRAFT_KEY, None)
    next_state = upsert_proactive_account_state(
        account_id=account_id,
        metadata=metadata,
    )
    return {
        "action": "promoted_candidate",
        "account_id": account_id,
        "candidate": candidate,
        "promoted_at": _format_decision_time(current),
        "proactive_state": next_state,
    }


def clear_account_check_candidate_draft(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = now or datetime.now()
    state = get_proactive_account_state(account_id=account_id)
    if state is None:
        return _no_op(account_id=account_id, reason="proactive_state_missing", now=current)

    metadata = dict(state.get("metadata") or {})
    had_draft = (
        ACCOUNT_CHECK_CANDIDATE_DRAFT_KEY in metadata
        or LEGACY_HEARTBEAT_CANDIDATE_DRAFT_KEY in metadata
    )
    metadata.pop(ACCOUNT_CHECK_CANDIDATE_DRAFT_KEY, None)
    metadata.pop(LEGACY_HEARTBEAT_CANDIDATE_DRAFT_KEY, None)
    metadata["account_check_candidate_draft_cleared_at"] = _format_decision_time(current)
    next_state = upsert_proactive_account_state(
        account_id=account_id,
        metadata=metadata,
    )
    return {
        "action": "cleared_candidate_draft",
        "account_id": account_id,
        "had_draft": had_draft,
        "cleared_at": _format_decision_time(current),
        "proactive_state": next_state,
    }
