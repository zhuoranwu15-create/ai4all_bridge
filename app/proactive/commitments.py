import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.config import settings
from app.time_utils import beijing_naive_now
from app.db import (
    cancel_proactive_commitment,
    claim_due_proactive_commitment,
    create_proactive_commitment,
    get_account,
    get_proactive_account_state,
    list_channel_bindings_for_account,
    list_due_proactive_commitments,
    list_recent_messages,
    mark_proactive_commitment_failed,
    mark_proactive_commitment_sent,
)
from app.llm import generate_completion, is_llm_configured
from app.proactive.messaging import dispatch_proactive_text
from app.proactive.state import mark_account_proactive_sent


COMMITMENT_EXTRACTION_SYSTEM_PROMPT = """你是 AI4ALL 的隐藏 follow-up commitment 抽取器。

你的任务是判断本轮对话是否出现了一个明确、低打扰、高价值、适合未来主动跟进的事项。

严格规则：
- 只输出 JSON，不输出解释，不输出 Markdown。
- 默认不创建 commitment；没有明确后续价值时 should_create=false。
- 不要抽取普通寒暄、情绪陪伴、泛泛建议、无时间约束的事项。
- 不要编造用户没有表达过的目标、事实、日期或后续动作。
- 不抽取医疗、法律、金融等高风险建议。
- 如果用户已经显式要求“提醒我”，主提醒链路会处理，这里不要重复抽取。
- due_at 必须是未来时间，格式为 YYYY-MM-DD HH:MM:SS。
- text 必须是未来主动发给用户的一条自然微信消息，最多 80 个中文字符。

JSON schema:
{
  "should_create": false,
  "text": "",
  "due_at": "",
  "reason": "简短原因",
  "confidence": 0.0
}
"""


def _format_time(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


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
        "evaluated_at": _format_time(now),
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


def _parse_due_at(value: Any) -> Optional[datetime]:
    text = _clean_text(value)
    if not text:
        return None
    normalized = text.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None


def _build_extraction_user_prompt(
    *,
    account_id: str,
    user_text: str,
    assistant_text: str,
    recent_history: List[Dict[str, str]],
    now: datetime,
) -> str:
    history_lines = [
        f"- {item['role']}: {_truncate_text(item.get('content') or '', 240)}"
        for item in recent_history
        if _clean_text(item.get("content"))
    ]
    return "\n\n".join(
        [
            f"now: {_format_time(now)}",
            f"account_id: {account_id}",
            "current_turn_user:\n" + _truncate_text(user_text, 800),
            "current_turn_assistant:\n" + _truncate_text(assistant_text, 800),
            "recent_chat:\n" + ("\n".join(history_lines) if history_lines else "- none"),
        ]
    )


def _normalize_commitment_payload(
    payload: Dict[str, Any],
    *,
    now: datetime,
) -> Optional[Dict[str, Any]]:
    if payload.get("should_create") is not True:
        return None
    text = _clean_text(payload.get("text"))
    if not text:
        return None
    due_at = _parse_due_at(payload.get("due_at"))
    if due_at is None or due_at <= now:
        return None
    max_days = int(getattr(settings, "proactive_commitment_max_days", 14) or 14)
    if due_at > now + timedelta(days=max(max_days, 1)):
        return None
    confidence = float(payload.get("confidence") or 0)
    min_confidence = float(
        getattr(settings, "proactive_commitment_min_confidence", 0.9) or 0.9
    )
    if confidence < min_confidence:
        return None
    return {
        "text": text[:120],
        "due_at": _format_time(due_at),
        "reason": _clean_text(payload.get("reason")) or "commitment_extracted",
        "confidence": confidence,
    }


def extract_commitment_from_turn(
    *,
    account_id: str,
    session_id: int,
    user_text: str,
    assistant_text: str,
    source_message_id: Optional[str],
    source_reply_message_id: Optional[str],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = now or beijing_naive_now()

    if not getattr(settings, "proactive_commitment_extraction_enabled", True):
        return _no_op(account_id=account_id, reason="commitment_extraction_disabled", now=current)

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

    if not is_llm_configured():
        return _no_op(account_id=account_id, reason="llm_disabled", now=current)

    if _select_route(account_id) is None:
        return _no_op(account_id=account_id, reason="missing_channel_route", now=current)

    cleaned_user = _clean_text(user_text)
    cleaned_assistant = _clean_text(assistant_text)
    if not cleaned_user or not cleaned_assistant:
        return _no_op(account_id=account_id, reason="empty_turn", now=current)

    history = list_recent_messages(
        session_id=session_id,
        limit=int(getattr(settings, "proactive_commitment_context_messages", 8) or 8),
    )
    messages = [
        {"role": "system", "content": COMMITMENT_EXTRACTION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _build_extraction_user_prompt(
                account_id=account_id,
                user_text=cleaned_user,
                assistant_text=cleaned_assistant,
                recent_history=history,
                now=current,
            ),
        },
    ]

    try:
        raw = generate_completion(messages)
        payload = _extract_json_object(raw)
        commitment = _normalize_commitment_payload(payload, now=current)
    except Exception as err:
        return _no_op(
            account_id=account_id,
            reason="commitment_extraction_failed",
            now=current,
            metadata={"error": str(err)},
        )

    if commitment is None:
        return _no_op(account_id=account_id, reason="llm_no_commitment", now=current)

    dedupe_source = (
        _clean_text(source_message_id)
        or _clean_text(source_reply_message_id)
        or _format_time(current)
    )
    item = create_proactive_commitment(
        account_id=account_id,
        session_id=session_id,
        source_message_id=source_message_id,
        source_reply_message_id=source_reply_message_id,
        text=commitment["text"],
        due_at=commitment["due_at"],
        confidence=commitment["confidence"],
        reason=commitment["reason"],
        dedupe_key=f"commitment:{account_id}:{dedupe_source}",
        metadata={
            "source": "hidden_commitment_extractor_v1",
            "extracted_at": _format_time(current),
        },
    )
    return {
        "action": "created_commitment",
        "account_id": account_id,
        "commitment": item,
        "evaluated_at": _format_time(current),
    }


def dispatch_commitment(
    *,
    commitment_id: str,
    now: Optional[datetime] = None,
    bypass_quiet_hours: bool = False,
) -> Dict[str, Any]:
    current = now or beijing_naive_now()
    claimed = claim_due_proactive_commitment(
        commitment_id=commitment_id,
        now=_format_time(current),
    )
    if claimed is None:
        return {
            "status": "skipped",
            "reason": "not_due_or_already_claimed",
            "commitment_id": commitment_id,
        }

    route = _select_route(claimed["account_id"])
    if route is None:
        commitment = mark_proactive_commitment_failed(
            commitment_id=claimed["id"],
            error="missing_channel_route",
        )
        return {
            "status": "failed",
            "reason": "missing_channel_route",
            "commitment": commitment,
        }

    try:
        outbound = dispatch_proactive_text(
            account_id=claimed["account_id"],
            channel=route["channel"],
            channel_account_id=route.get("channel_account_id"),
            to_user_id=route["to_user_id"],
            session_key=route.get("session_key"),
            source="commitment",
            text=claimed["text"],
            idempotency_key=f"commitment-{claimed['id']}",
            now=current,
            bypass_quiet_hours=bypass_quiet_hours,
            product_category="companion_followup",
            metadata={
                "commitment_id": claimed["id"],
                "commitment_due_at": claimed["due_at"],
                "commitment_reason": claimed.get("reason"),
                "commitment_confidence": claimed.get("confidence"),
                "channel_binding_id": route.get("channel_binding_id"),
            },
        )
    except Exception as err:
        commitment = mark_proactive_commitment_failed(
            commitment_id=claimed["id"],
            error=str(err),
        )
        return {
            "status": "failed",
            "commitment": commitment,
            "error": str(err),
        }

    outbound_id = int(outbound["id"]) if outbound.get("id") is not None else None
    outbound_status = outbound.get("status")
    if outbound_status == "sent":
        commitment = mark_proactive_commitment_sent(
            commitment_id=claimed["id"],
            outbound_message_id=outbound_id,
        )
        mark_account_proactive_sent(
            account_id=claimed["account_id"],
            now=current,
        )
        return {
            "status": "sent",
            "commitment": commitment,
            "outbound_message": outbound,
        }
    if outbound_status == "cancelled":
        commitment = cancel_proactive_commitment(
            commitment_id=claimed["id"],
            outbound_message_id=outbound_id,
            error=outbound.get("error") or "outbound_cancelled",
        )
        return {
            "status": "cancelled",
            "commitment": commitment,
            "outbound_message": outbound,
        }

    commitment = mark_proactive_commitment_failed(
        commitment_id=claimed["id"],
        outbound_message_id=outbound_id,
        error=outbound.get("error") or f"outbound_status:{outbound_status}",
    )
    return {
        "status": "failed",
        "commitment": commitment,
        "outbound_message": outbound,
    }


def dispatch_due_commitments(
    *,
    now: Optional[datetime] = None,
    limit: int = 20,
    bypass_quiet_hours: bool = False,
    node_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    current = now or beijing_naive_now()
    due = list_due_proactive_commitments(
        now=_format_time(current),
        limit=limit,
        node_id=node_id,
    )
    results: List[Dict[str, Any]] = []
    for item in due:
        results.append(
            dispatch_commitment(
                commitment_id=item["id"],
                now=current,
                bypass_quiet_hours=bypass_quiet_hours,
            )
        )
    return results
