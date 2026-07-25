"""**admin 手工关怀候选**（manual companion）的生成（draft/promote/clear）。

语义归位（阶段2.5）：本模块即历史上的 "account_check" 候选生成器。它**只由 admin 端点**
人工产生候选（draft → promote），自动调度链路（planning）**从不生成**它，故 scheduler 的
account_checks 步在无人工候选时恒 no_op —— 这是有意设计的休眠路径，不是漏接。自动在跑的
自主外联是 topic_followup / content_invitation（见 generation 包说明）。

为何函数名与 metadata key 仍带 `account_check`：候选落在 account_state metadata 的
`account_check_candidate` 键，是已落库的**不可变数据契约**，故 `*_account_check_*` 函数名与
该 key 一并保留（renaming 需数据迁移，超出本轮"行为等价"边界）。模块名已归位为
manual_companion 以诚实表达其"admin 手工关怀"角色。
"""
import json
from datetime import datetime
from typing import Any, Dict, Optional

from app.config import settings
from app.time_utils import beijing_naive_now
from app.db import get_account, get_proactive_account_state, upsert_proactive_account_state
from app.agent_runtime.llm.service import generate_completion, is_llm_configured
from app.agent_runtime.llm.providers import TASK_PROACTIVE_RECALL, tier_for_task
from app.proactive.contract.common import _clean_text, _extract_json_object, _select_route, _truncate_text
from app.proactive.delivery.touch_state import STALE, get_account_touch_state
from app.products.zhaoxi.infrastructure.profiles import read_agent_context
from app.proactive.contract.prompts import ACCOUNT_CHECK_CANDIDATE_SYSTEM_PROMPT
from app.proactive.recall._shared import (
    ACCOUNT_CHECK_CANDIDATE_DRAFT_KEY,
    ACCOUNT_CHECK_CANDIDATE_KEY,
    LEGACY_HEARTBEAT_CANDIDATE_DRAFT_KEY,
    LEGACY_HEARTBEAT_CANDIDATE_KEY,
    _format_decision_time,
    _latest_session_history,
    _no_op,
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

def generate_account_check_candidate_draft(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
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

    if not is_llm_configured(tier_for_task(TASK_PROACTIVE_RECALL)):
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
        raw = generate_completion(messages, tier=tier_for_task(TASK_PROACTIVE_RECALL))
        payload = _extract_json_object(raw)
        candidate = _normalize_llm_candidate(payload, now=current)
    except Exception as err:
        return _no_op(
            account_id=account_id,
            reason="candidate_generation_failed",
            now=current,
            metadata={"error": str(err)},
        )

    if candidate is None:
        upsert_proactive_account_state(
            account_id=account_id,
            metadata_patch={
                "account_check_candidate_draft_generated_at": _format_decision_time(current),
                ACCOUNT_CHECK_CANDIDATE_DRAFT_KEY: None,
                LEGACY_HEARTBEAT_CANDIDATE_DRAFT_KEY: None,
            },
        )
        return _no_op(account_id=account_id, reason="llm_no_candidate", now=current)

    next_state = upsert_proactive_account_state(
        account_id=account_id,
        metadata_patch={
            "account_check_candidate_draft_generated_at": _format_decision_time(current),
            ACCOUNT_CHECK_CANDIDATE_DRAFT_KEY: candidate,
            LEGACY_HEARTBEAT_CANDIDATE_DRAFT_KEY: None,
        },
    )
    return {
        "action": "draft_candidate",
        "account_id": account_id,
        "candidate": candidate,
        "evaluated_at": _format_decision_time(current),
        "proactive_state": next_state,
    }

def promote_account_check_candidate_draft(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = now or beijing_naive_now()
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

    next_state = upsert_proactive_account_state(
        account_id=account_id,
        metadata_patch={
            ACCOUNT_CHECK_CANDIDATE_KEY: candidate,
            "account_check_candidate_promoted_at": _format_decision_time(current),
            LEGACY_HEARTBEAT_CANDIDATE_KEY: None,
            ACCOUNT_CHECK_CANDIDATE_DRAFT_KEY: None,
            LEGACY_HEARTBEAT_CANDIDATE_DRAFT_KEY: None,
        },
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
    current = now or beijing_naive_now()
    state = get_proactive_account_state(account_id=account_id)
    if state is None:
        return _no_op(account_id=account_id, reason="proactive_state_missing", now=current)

    metadata = state.get("metadata") or {}
    had_draft = (
        ACCOUNT_CHECK_CANDIDATE_DRAFT_KEY in metadata
        or LEGACY_HEARTBEAT_CANDIDATE_DRAFT_KEY in metadata
    )
    next_state = upsert_proactive_account_state(
        account_id=account_id,
        metadata_patch={
            ACCOUNT_CHECK_CANDIDATE_DRAFT_KEY: None,
            LEGACY_HEARTBEAT_CANDIDATE_DRAFT_KEY: None,
            "account_check_candidate_draft_cleared_at": _format_decision_time(current),
        },
    )
    return {
        "action": "cleared_candidate_draft",
        "account_id": account_id,
        "had_draft": had_draft,
        "cleared_at": _format_decision_time(current),
        "proactive_state": next_state,
    }
