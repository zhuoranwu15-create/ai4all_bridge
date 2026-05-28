import json
from datetime import datetime
from typing import Any, Dict, Optional

from app.config import settings
from app.db import (
    get_account,
    get_outbound_daily_usage,
    get_proactive_account_state,
    list_recent_messages,
    list_channel_bindings_for_account,
    list_sessions_for_account,
    upsert_proactive_account_state,
)
from app.llm import generate_completion
from app.proactive.messaging import is_quiet_hours, send_proactive_text
from app.user_profiles import read_agent_context, read_daily_notes


HEARTBEAT_CANDIDATE_SYSTEM_PROMPT = """你是 AI4ALL 的隐藏 heartbeat 候选生成器。

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
    candidate = metadata.get("heartbeat_candidate")
    if isinstance(candidate, dict):
        text = _clean_text(candidate.get("text"))
        if text:
            return {
                "id": _clean_text(candidate.get("id")) or "metadata",
                "text": text,
                "source": _clean_text(candidate.get("source")) or "metadata",
                "reason": _clean_text(candidate.get("reason")) or "manual_candidate",
            }

    text = _clean_text(metadata.get("heartbeat_candidate_text"))
    if text:
        return {
            "id": _clean_text(metadata.get("heartbeat_candidate_id")) or "metadata",
            "text": text,
            "source": _clean_text(metadata.get("heartbeat_candidate_source")) or "metadata",
            "reason": _clean_text(metadata.get("heartbeat_candidate_reason")) or "manual_candidate",
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
    today = now.date().isoformat()
    agent_context = read_agent_context(
        account_id,
        display_name=account.get("display_name"),
    )
    daily_notes = read_daily_notes(account_id, today)
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
            "daily_notes:\n" + _truncate_text(daily_notes or "", 1200),
            "proactive_state_metadata:\n"
            + _truncate_text(json.dumps(state.get("metadata") or {}, ensure_ascii=False), 1200),
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
        getattr(settings, "proactive_heartbeat_candidate_min_confidence", 0.85)
        or 0.85
    )
    if confidence < min_confidence:
        return None
    return {
        "id": "llm-" + now.strftime("%Y%m%d%H%M%S"),
        "text": text[:120],
        "source": "heartbeat_llm_candidate_v1",
        "reason": _clean_text(payload.get("reason")) or "llm_candidate",
        "confidence": confidence,
    }


def decide_heartbeat_action(
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

    if not getattr(settings, "proactive_outbound_enabled", True):
        return _no_op(account_id=account_id, reason="proactive_outbound_disabled", now=current)

    if is_quiet_hours(
        now=current,
        start=getattr(settings, "proactive_quiet_hours_start", "22:00"),
        end=getattr(settings, "proactive_quiet_hours_end", "08:00"),
    ):
        return _no_op(account_id=account_id, reason="quiet_hours", now=current)

    max_per_day = int(getattr(settings, "proactive_outbound_daily_limit", 0) or 0)
    quota_date = current.date().isoformat()
    if max_per_day > 0:
        current_count = get_outbound_daily_usage(
            account_id=account_id,
            quota_date=quota_date,
        )
        if current_count >= max_per_day:
            return _no_op(
                account_id=account_id,
                reason="daily_limit_exceeded",
                now=current,
                metadata={
                    "daily_count": current_count,
                    "daily_limit": max_per_day,
                    "quota_date": quota_date,
                },
            )

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
        "source": "heartbeat",
        "text": candidate["text"],
        "idempotency_key": f"heartbeat-{account_id}-{candidate['id']}-{quota_date}",
        "route": route,
        "candidate": candidate,
        "evaluated_at": current_text,
        "metadata": {
            "quota_date": quota_date,
            "decision": "metadata_candidate",
        },
    }


def execute_heartbeat_decision(
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
        source=decision.get("source") or "heartbeat",
        text=decision["text"],
        idempotency_key=decision.get("idempotency_key"),
        now=now,
        bypass_quiet_hours=False,
        metadata={
            **(decision.get("metadata") or {}),
            "heartbeat_candidate": decision.get("candidate") or {},
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


def generate_heartbeat_candidate_draft(
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
        limit=int(getattr(settings, "proactive_heartbeat_candidate_context_messages", 12) or 12),
    )
    if not history:
        return _no_op(account_id=account_id, reason="no_recent_history", now=current)

    messages = [
        {"role": "system", "content": HEARTBEAT_CANDIDATE_SYSTEM_PROMPT},
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
    next_metadata["heartbeat_candidate_draft_generated_at"] = _format_decision_time(current)
    if candidate is None:
        next_metadata.pop("heartbeat_candidate_draft", None)
        upsert_proactive_account_state(
            account_id=account_id,
            metadata=next_metadata,
        )
        return _no_op(account_id=account_id, reason="llm_no_candidate", now=current)

    next_metadata["heartbeat_candidate_draft"] = candidate
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


def promote_heartbeat_candidate_draft(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = now or datetime.now()
    state = get_proactive_account_state(account_id=account_id)
    if state is None:
        return _no_op(account_id=account_id, reason="proactive_state_missing", now=current)

    metadata = dict(state.get("metadata") or {})
    draft = metadata.get("heartbeat_candidate_draft")
    if not isinstance(draft, dict) or not _clean_text(draft.get("text")):
        return _no_op(account_id=account_id, reason="draft_missing", now=current)

    candidate = {
        "id": _clean_text(draft.get("id")) or "draft",
        "text": _clean_text(draft.get("text")),
        "source": _clean_text(draft.get("source")) or "heartbeat_draft",
        "reason": _clean_text(draft.get("reason")) or "promoted_draft",
    }
    if draft.get("confidence") is not None:
        candidate["confidence"] = draft.get("confidence")

    metadata["heartbeat_candidate"] = candidate
    metadata["heartbeat_candidate_promoted_at"] = _format_decision_time(current)
    metadata.pop("heartbeat_candidate_draft", None)
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


def clear_heartbeat_candidate_draft(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = now or datetime.now()
    state = get_proactive_account_state(account_id=account_id)
    if state is None:
        return _no_op(account_id=account_id, reason="proactive_state_missing", now=current)

    metadata = dict(state.get("metadata") or {})
    had_draft = "heartbeat_candidate_draft" in metadata
    metadata.pop("heartbeat_candidate_draft", None)
    metadata["heartbeat_candidate_draft_cleared_at"] = _format_decision_time(current)
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
