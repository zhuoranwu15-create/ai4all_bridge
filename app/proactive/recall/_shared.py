"""account_check 生成/派发共享：候选常量 + 元数据读取 + 时间/会话小工具（无 settings 依赖）。"""
from datetime import datetime
from typing import Any, Dict, Optional

from app.db import list_recent_messages, list_sessions_for_account
from app.proactive.contract.common import _clean_text


ACCOUNT_CHECK_SOURCE = "account_check"
ACCOUNT_CHECK_CANDIDATE_KEY = "account_check_candidate"
ACCOUNT_CHECK_CANDIDATE_DRAFT_KEY = "account_check_candidate_draft"
LEGACY_HEARTBEAT_CANDIDATE_KEY = "heartbeat_candidate"
LEGACY_HEARTBEAT_CANDIDATE_DRAFT_KEY = "heartbeat_candidate_draft"


def _format_decision_time(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")

def _parse_state_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None

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
