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
    list_recent_messages,
    list_recent_messages_for_account_since,
    list_recent_reactivation_outbound_messages,
    list_channel_bindings_for_account,
    list_sessions_for_account,
    upsert_proactive_account_state,
)
from app.llm import generate_completion, generate_reply_with_tools
from app.proactive.messaging import send_proactive_text
from app.proactive.reactivation import (
    REACTIVATION_TYPE_TOPIC_FOLLOWUP,
)
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


TOPIC_FOLLOWUP_SYSTEM_PROMPT = """你是 AI4ALL 的隐藏 topic_followup 拉活候选生成器。

你的任务是基于用户最近 72 小时的聊天，判断是否适合生成一句朋友式续聊消息。

topic_followup 适合：
- 相亲、亲子、情绪、关系、社交压力、角色陪伴、日常经历等个人话题。
- 用户昨天或最近聊过一件具体的事，今天可以自然关心一句后续。
- 用户表达过希望 AI 主动联系、想被惦记、想继续聊，且最近有一个可自然接上的关系/陪伴话题。
- 用户聊到“一个人吃饭不香”“不知道怎么和异性聊天”“最近有点孤单/纠结”等轻量社交或情绪状态，可以用一句低压力续聊打开话题。
- 生成内容要像熟悉的朋友顺手问候，不像客服、任务提醒或文章推荐。

topic_followup 不适合：
- 新闻、轻知识、地理科普、体育赛事、公开资料、内容标题推荐；这些应交给 content_invitation。
- 医疗、法律、金融投资建议。
- 纯寒暄、身份设定闲聊、没有具体可延续事件的话题。

严格规则：
- 只输出 JSON，不输出解释，不输出 Markdown。
- 默认不主动打扰；没有自然续聊点时 should_send=false。
- 只基于输入中的最近 72 小时聊天生成候选，不要主动翻长期旧事。
- 可以参考 USER/SOUL 的称呼和语气，但不要编造事实、日期、承诺或用户目标。
- text 必须短、自然、像微信消息，最多 80 个中文字符。
- 不要说“我找到几篇/几条内容/文章”，不要生成标题列表。
- 生成的是今日稍后可发送的候选；不要因为当前执行时间是凌晨、quiet hours、用户刚说过晚安而拒绝，这些由调度层决定。
- 只要有具体可延续点，就可以生成一句轻量候选；不要用“最后以晚安结束”作为唯一拒绝理由。
- 如果 recent_sent_reactivations 非空，不要生成与其中任何条目 topic 相同或高度相似的候选；请选择别的自然续聊点，或在没有更好选项时返回 should_send=false。

JSON schema:
{
  "should_send": false,
  "text": "",
  "topic": "简短主题",
  "reason": "简短原因",
  "confidence": 0.0
}
"""


CONTENT_INVITATION_SYSTEM_PROMPT = """你是 AI4ALL 的隐藏内容邀请候选生成器。

你的任务是判断当前账号是否存在一个低打扰、和用户最近聊天相关、适合朋友式询问的内容邀请候选。

content_invitation 只负责“可以发几条内容/标题给用户看看”的场景。
它不是普通关心、不是追问用户个人近况，也不是情绪陪伴。

适合创建候选的情况：
- 新闻、轻知识、地理科普、体育赛事、公开资料、AI 产品/技术、出行攻略、天气/露营准备等内容型话题。
- 用户主动问过某类资讯、知识或资料，或最近聊天表现出对某个外部主题的兴趣。
- 候选内容可以是轻量文章标题、资料标题、观点合集、方法清单或科普标题。

严格规则：
- 必须通过工具完成动作：适合时调用 create_content_invitation_candidate，不适合时调用 skip_content_invitation。
- 只做一次最终动作：调用 create_content_invitation_candidate 或 skip_content_invitation 后，不要再连续调用其他工具。
- 不使用关键词机械触发；必须能说明用户最近为什么可能想看这些内容。
- 相亲、亲子、情绪、关系、社交压力、角色陪伴、日常经历这类个人续聊话题必须 skip，交给 topic_followup。
- 主动邀请必须紧贴用户最近聊过的具体内容，不能泛泛推“今日热点”“每日鸡汤”。
- 主动邀请文本只能是短的询问句，不能包含标题、URL、来源链接、长摘要或日报式表达。
- title_items 至少 3 条，最多 10 条；发给用户前只会展示 title。
- 如果没有可靠来源或没有联网搜索结果，title_items 可以只是自然的内容标题/话题标题；不要编造 source_name、url 或 published_at。
- 不要创建医疗、法律、金融投资等高风险内容邀请。
- 涉及金融时只能做制度、常识或公开背景科普，不能给个股、行情、买卖建议。
- 不要仅因为当前时间较晚或处于 quiet hours 而跳过；发送时机、quiet hours 和日上限由后端策略处理。
- 只有在最近对话没有合适的内容/标题候选，或候选会显得突兀/冒犯时，才调用 skip_content_invitation。
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


def _build_topic_followup_user_prompt(
    *,
    account: Dict[str, Any],
    state: Dict[str, Any],
    history: list[dict[str, Any]],
    now: datetime,
    recent_sent_topics: Optional[list] = None,
) -> str:
    account_id = str(account["id"])
    agent_context = read_agent_context(
        account_id,
        display_name=account.get("display_name"),
    )
    context_blocks = agent_context.blocks
    history_lines = [
        (
            f"- #{item.get('id')} {item.get('created_at')} "
            f"{item['role']}: {_truncate_text(item.get('content') or '', 300)}"
        )
        for item in history
        if _clean_text(item.get("content"))
    ]
    sent_lines = (
        "\n".join(
            f"- {item.get('sent_at') or item.get('created_at')} topic={item.get('topic')} text={_truncate_text(item.get('text') or '', 80)}"
            for item in (recent_sent_topics or [])
        )
        or "- none"
    )
    return "\n\n".join(
        [
            f"now: {_format_decision_time(now)}",
            f"account_id: {account_id}",
            "SOUL.md:\n" + _truncate_text(context_blocks.get("SOUL", ""), 1000),
            "USER.md:\n" + _truncate_text(context_blocks.get("USER", ""), 1200),
            "proactive_state_metadata:\n"
            + _truncate_text(json.dumps(state.get("metadata") or {}, ensure_ascii=False), 1200),
            "recent_sent_reactivations:\n" + sent_lines,
            "recent_72h_chat:\n" + ("\n".join(history_lines) if history_lines else "- none"),
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


def _normalize_topic_followup_candidate(
    payload: Dict[str, Any],
    *,
    now: datetime,
    source_message_cutoff_id: Optional[int],
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
    # Include microseconds so a regenerate triggered within the same wall-clock
    # second produces a different candidate id and a different outbound
    # idempotency_key (reactivation-{account}-{id}-{date}).
    candidate: Dict[str, Any] = {
        "id": "reactivation-topic-" + now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond:06d}",
        "type": REACTIVATION_TYPE_TOPIC_FOLLOWUP,
        "text": text[:120],
        "topic": _clean_text(payload.get("topic")) or "topic_followup",
        "reason": _clean_text(payload.get("reason")) or "topic_followup_llm_candidate",
        "confidence": confidence,
        "generated_at": _format_decision_time(now),
    }
    if source_message_cutoff_id is not None:
        candidate["source_message_cutoff_id"] = source_message_cutoff_id
    return candidate


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
    current = now or beijing_naive_now()

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


def generate_topic_followup_candidate(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Generate one topic_followup reactivation candidate from recent account chat."""
    current = now or beijing_naive_now()

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

    try:
        window_hours = int(getattr(settings, "reactivation_topic_followup_window_hours", 72) or 72)
    except (TypeError, ValueError):
        window_hours = 72
    try:
        context_limit = int(getattr(settings, "reactivation_topic_followup_context_messages", 100) or 100)
    except (TypeError, ValueError):
        context_limit = 100

    # messages.created_at 存北京时间（insert 时 datetime('now','+8 hours')），current 也是
    # 北京 naive 时间，直接按北京时间比较，**不做 UTC 转换**（旧 local_to_utc_string 会把
    # 阈值偏移一个时区，导致窗口错位）。
    since_local = _format_decision_time(current - timedelta(hours=max(window_hours, 1)))
    history = list_recent_messages_for_account_since(
        account_id=account_id,
        since=since_local,
        limit=max(1, context_limit),
    )
    if not history:
        return _no_op(
            account_id=account_id,
            reason="no_recent_72h_history",
            now=current,
            metadata={"since": since_local},
        )

    try:
        dedupe_days = int(getattr(settings, "reactivation_dedupe_days", 3) or 3)
    except (TypeError, ValueError):
        dedupe_days = 3
    # outbound_messages.created_at 同为北京时间，直接按北京时间比较，不做 UTC 转换。
    since_dedupe = _format_decision_time(current - timedelta(days=max(dedupe_days, 1)))
    sent_history = list_recent_reactivation_outbound_messages(
        account_id=account_id,
        since=since_dedupe,
        limit=20,
    )
    recent_sent_topics = [
        {
            "sent_at": item.get("sent_at") or item.get("created_at"),
            "topic": (item.get("metadata") or {}).get("topic"),
            "text": item.get("text"),
        }
        for item in sent_history
        if (item.get("metadata") or {}).get("topic")
    ]

    messages = [
        {"role": "system", "content": TOPIC_FOLLOWUP_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _build_topic_followup_user_prompt(
                account=account,
                state=state,
                history=history,
                now=current,
                recent_sent_topics=recent_sent_topics,
            ),
        },
    ]
    try:
        raw = generate_completion(messages)
        payload = _extract_json_object(raw)
        source_cutoff = max((int(item["id"]) for item in history if item.get("id") is not None), default=None)
        candidate = _normalize_topic_followup_candidate(
            payload,
            now=current,
            source_message_cutoff_id=source_cutoff,
        )
    except Exception as err:
        return _no_op(
            account_id=account_id,
            reason="topic_followup_generation_failed",
            now=current,
            metadata={"error": str(err)},
        )

    if candidate is None:
        return _no_op(
            account_id=account_id,
            reason="llm_no_topic_followup_candidate",
            now=current,
            metadata={"reply": _truncate_text(raw or "", 400)},
        )

    return {
        "action": "topic_followup_candidate_created",
        "account_id": account_id,
        "reactivation_candidate": candidate,
        "evaluated_at": _format_decision_time(current),
    }


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
