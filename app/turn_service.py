import asyncio
import logging
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.time_utils import beijing_now, beijing_daypart_str, beijing_weekday_str
from typing import Any, Dict, List, Optional, Union

from app.config import settings
from app.db import (
    ACCOUNT_ACTIVE_SESSION_KEY,
    clear_session_messages,
    connect as db_connect,
    count_context_messages_for_session,
    get_account_onboarding_state,
    get_active_content_invitation,
    get_daily_usage,
    get_duplicate_reply,
    increment_session_turn_count,
    increment_daily_usage,
    insert_debug_trace,
    insert_message,
    list_recent_messages_for_account,
    mark_message_moderation_blocked,
    process_referral_message_for_account,
    record_chat_usage_charge,
    record_image_understanding_charge,
    resolve_account_id_for_inbound_channel_identity,
    resolve_node_for_account,
    set_account_onboarding_state,
    upsert_channel_binding,
)
from app.identity import identity_response_metadata, resolve_openclaw_identity
from app.image_understanding import describe_image
from app.llm import generate_reply, generate_reply_with_tools, resolve_active_llm_provider
from app.llm_providers import LLMProviderConfig
from app.memory_writer import write_memory
from app.relationship_state import maybe_update_relationship_state_after_turn
from app.moderation.sensitive_words import check_sync_guard
from app.moderation.service import (
    create_sync_block_task,
    enqueue_message_for_moderation,
    screen_inbound_message_sync,
)
from app.prompt_builder import PromptBuilder, extract_section
from app.proactive.commitments import extract_commitment_from_turn
from app.proactive.state import ensure_account_state
from app.rate_limiter import rate_limiter
from app.schemas import OpenClawTurnRequest, OpenClawTurnResponse
from app.tools import get_default_tools, iter_specs
from app.turn_context import TurnContext
from app.session_lifecycle import business_day_for, get_or_create_account_active_session_with_dreaming
from app.onboarding import (
    apply_extracted_onboarding_info,
    build_onboarding_prompt_context,
    extract_onboarding_info_async,
    is_onboarding_active,
    next_onboarding_state,
    ONBOARDING_COMPLETE,
    ONBOARDING_PENDING,
    ONBOARDING_STEP1_SENT,
    ONBOARDING_STEP2_SENT,
    ONBOARDING_STEP3_SENT,
    ONBOARDING_WELCOME_TEXT,
)
from app import node_gateway
from app.user_profiles import (
    ensure_agent_context_files,
    ensure_user_profile,
    read_agent_context,
    read_user_profile,
)


logger = logging.getLogger("ai4all.turn_service")

_SPECIAL_COMMANDS = {"#重置会话", "#状态"}
_GENERATION_ERROR_REPLY = "我这边刚刚有点卡住了，你可以稍后再发我一次。"

_TOOL_THINKING_MSGS_SEARCH = [
    "稍等，我查一下~",
    "我去找找，马上回来~",
    "让我搜索一下，你别着急~",
]
_TOOL_THINKING_MSGS_DEFAULT = [
    "稍等，我想想~",
    "让我想一想，你别急~",
    "嗯，我考虑一下~",
]


def _make_tool_thinking_sender(
    *,
    identity,
    account_id: str,
    openclaw_session_key: str,
) -> Optional[Any]:
    """返回一个 fire-and-forget 闭包：当 LLM 首次触发工具调用时，先发一条"思考中"消息给用户。

    best-effort。统一经 node_gateway.node_send_text 按账号归属节点发送：本机账号直调 openclaw，
    远程账号 HTTP push 到归属节点即时发（不再因「非本机」静默跳过，local/remote 行为一致）。
    远程不可达等异常静默丢弃（暂态提示可丢，不进队列、不落库、不进审计/计费）。
    """
    to_user_id = (identity.chat_id or identity.sender_id or "").strip()
    if not to_user_id:
        return None

    def _send(tool_names: List[str]) -> None:
        is_search = any("search" in (n or "").lower() for n in tool_names)
        pool = _TOOL_THINKING_MSGS_SEARCH if is_search else _TOOL_THINKING_MSGS_DEFAULT
        msg = random.choice(pool)
        # 计时锚点:回调触发(=工具检测)时刻,用于和 tool_thinking_sent 的 elapsed_ms 对比拆解延迟。
        logger.info("tool_thinking_dispatch account=%s tools=%s", account_id, tool_names)
        _dispatch_at = time.monotonic()

        def _do_send() -> None:
            # 纯 UX 暂态提示：不落 messages 表，不进审计/计费链路，不出现在 LLM 对话历史中。
            try:
                node_gateway.node_send_text(
                    # 归属解析与 enqueue 一致:账号归属 > default_node_id;空 → 本机兜底。
                    node_id=resolve_node_for_account(account_id) or (settings.default_node_id or None),
                    to_user_id=to_user_id,
                    text=msg,
                    gateway_timeout_ms=settings.openclaw_gateway_call_timeout_ms,
                    account_id=identity.channel_account_id,
                    session_key=openclaw_session_key,
                    channel=identity.channel,
                )
                logger.info(
                    "tool_thinking_sent account=%s tools=%s msg=%r elapsed_ms=%d",
                    account_id, tool_names, msg, int((time.monotonic() - _dispatch_at) * 1000),
                )
            except Exception as _err:
                logger.warning("tool_thinking send failed account=%s error=%s", account_id, _err)

        threading.Thread(target=_do_send, daemon=True).start()

    return _send


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def _record_timing(timings: Dict[str, int], key: str, started_at: float) -> int:
    value = _elapsed_ms(started_at)
    timings[key] = value
    return value


def _log_turn_timing(
    *,
    payload: OpenClawTurnRequest,
    timings: Dict[str, int],
    started_at: float,
    status: str,
    account_id: Optional[str] = None,
    session: Optional[str] = None,
    message_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Emit one structured-enough timing line per inbound turn without message text."""
    logger.info(
        "openclaw_turn timing account=%s session=%s message_id=%s status=%s "
        "total_ms=%s reply_ready_ms=%s timings=%s error=%s",
        account_id,
        session or payload.session_key,
        message_id or payload.message_id or payload.event_id,
        status,
        _elapsed_ms(started_at),
        timings.get("reply_ready_ms"),
        timings,
        error,
    )

# 检测用户是否在表达"更新主动消息设置"的意图。命中时主对话首轮强制对应工具，避免
# DeepSeek 先反问确认。**仅供主对话 turn 路径调用**：通用 LLM 入口 generate_reply_with_tools
# 不再内置此判定，主动消息生成路径也不调用它，从而不会把内嵌的历史聊天文本误判成当前
# 意图（曾导致强制一个该路径 tools 不含的工具 → DeepSeek 400）。
_PROACTIVE_COUNT_RE = r"(?:[0-9０-９]+|[一二两三四五六七八九十]+)"
_PROACTIVE_UPDATE_RE = re.compile(
    # frequency: "每天最多1条" / "总共2条" / "一周3次"
    rf"(每天|每日|一天|总共|一周|每周)\s*(最多|最少|只|就)?\s*发?\s*{_PROACTIVE_COUNT_RE}\s*(条|次)"
    # e.g. "条数改为3" / "上限设为2" / "改为3条"
    rf"|(条数|上限|频次).{{0,6}}(改为|设为|调整为|改|设|调|限|调整).{{0,8}}{_PROACTIVE_COUNT_RE}"
    rf"|(改为|设为|调整为|改成|设成).{{0,6}}{_PROACTIVE_COUNT_RE}.{{0,4}}(条|次)"
    rf"|{_PROACTIVE_COUNT_RE}.{{0,5}}(条|次).{{0,8}}(就够|就行|为限|上限|够了)"
    # on/off/mute
    r"|别(再|继续)?(主动|发).{0,10}(消息|找|发)"
    r"|(关掉?|开启?|暂停|停止|恢复).{0,6}主动"
    # action verbs only (exclude noun "设置")
    r"|主动消息.{0,10}(关闭|开启|暂停|停止|修改|调整|改为|设为|限制|减少|增加)"
    r"|(?:主动消息|主动|找我|联系我).{0,8}(?:多|少)发.{0,4}(?:点|些|次|条)"
    r"|(?:多|少)发.{0,4}(?:点|些|次|条).{0,8}(?:主动消息|主动找我|找我|联系我)"
    r"|总(共|量).{0,8}(条数|上限|改为|设为|[0-9０-９])",
    re.IGNORECASE,
)


def _infer_proactive_update_tool_choice(user_text: str) -> Any:
    """命中"主动设置更新"意图时返回强制 tool_choice dict，否则返回 "auto"。"""
    if _PROACTIVE_UPDATE_RE.search(str(user_text or "")):
        logger.debug("proactive_update_intent detected, forcing tool_choice")
        return {"type": "function", "function": {"name": "update_proactive_message_settings"}}
    return "auto"


def _ensure_pending_onboarding_question(reply: str) -> str:
    cleaned = (reply or "").strip()
    if "称呼你" in cleaned or "叫你" in cleaned or "喊你" in cleaned:
        return cleaned
    if not cleaned:
        return ONBOARDING_WELCOME_TEXT
    return f"{cleaned}\n\n{ONBOARDING_WELCOME_TEXT}"


def _extract_onboarding_info_sync(*, user_text: str, current_state: str) -> dict:
    return asyncio.run(
        extract_onboarding_info_async(
            user_text=user_text,
            current_state=current_state,
        )
    )


def _extract_ai_name_from_context(blocks: dict) -> Optional[str]:
    import re
    identity = blocks.get("IDENTITY", "")
    m = re.search(r"AI 名字[:：]\s*(.+)", identity)
    if m:
        return m.group(1).strip()
    return None


def _extract_user_name_from_context(blocks: dict) -> Optional[str]:
    import re
    user = blocks.get("USER", "")
    m = re.search(r"用户称呼[:：]\s*(.+)", user)
    if m:
        return m.group(1).strip()
    return None


def _count_user_name_asks(turn_count: int, state: str) -> int:
    """Approximate how many times we've asked the user's name so far."""
    if state in {ONBOARDING_STEP2_SENT, ONBOARDING_STEP3_SENT, ONBOARDING_COMPLETE}:
        return 1
    return 0


def _tool_instructions(
    *,
    active_content_invitation: Optional[dict] = None,
) -> str:
    if not active_content_invitation:
        return ""

    title_count = len(active_content_invitation.get("title_items") or [])
    instructions = [
        "## 当前内容邀请",
        "",
        f"- 当前存在待回应内容邀请：id={active_content_invitation['id']}，topic={active_content_invitation['topic']}，标题数={title_count}。",
        "- 本轮提供内容邀请回复工具：send_content_invitation_titles、record_content_invitation_feedback。",
    ]
    return "\n".join(instructions)


def _history_covers_previous_session(
    *,
    current_session_id: int,
    history_rows: List[Dict[str, Any]],
) -> bool:
    """Return True when history fully includes the latest prior session present."""
    included_by_session: Dict[int, int] = {}
    for row in history_rows:
        session_id = row.get("session_id")
        if session_id is None:
            continue
        session_id = int(session_id)
        if session_id == current_session_id:
            continue
        included_by_session[session_id] = included_by_session.get(session_id, 0) + 1
    if not included_by_session:
        return False

    previous_session_id = max(included_by_session)
    total = count_context_messages_for_session(session_id=previous_session_id)
    return total > 0 and included_by_session[previous_session_id] >= total


def _tool_name(schema: Dict[str, Any]) -> str:
    return str((schema.get("function") or {}).get("name") or "")


def _summarize_history_rows(history_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    session_ids = [
        int(row["session_id"])
        for row in history_rows
        if row.get("session_id") is not None
    ]
    return {
        "count": len(history_rows),
        "session_count": len(set(session_ids)),
        "messages": [
            {
                "id": row.get("id"),
                "message_id": row.get("message_id"),
                "session_id": row.get("session_id"),
                "role": row.get("role"),
                "chars": len(str(row.get("content") or "")),
            }
            for row in history_rows
        ],
    }


def _build_tooling_envelope(
    *,
    onboarding_active: bool,
    web_search_enabled: bool,
    active_content_invitation: Optional[dict],
    text: str,
    include_tool_instructions: bool,
) -> Dict[str, Any]:
    """Return tool schemas plus debug metadata for the main chat tool set."""
    content_invitation_enabled = bool(active_content_invitation)
    tools: List[Dict[str, Any]] = []
    available: List[Dict[str, Any]] = []
    disabled: List[Dict[str, Any]] = []
    enabled_names: set[str] = set()

    if not onboarding_active:
        tools = get_default_tools(
            web_search_enabled=web_search_enabled,
            content_invitation_response_enabled=content_invitation_enabled,
        )
        enabled_names = {_tool_name(schema) for schema in tools}

    for spec in iter_specs():
        if spec.default_when_flag == "never":
            continue
        if onboarding_active:
            disabled.append(
                {
                    "name": spec.name,
                    "group": spec.group,
                    "reason": "onboarding_active",
                }
            )
            continue
        if spec.name in enabled_names:
            reason = "default"
            if spec.default_when_flag == "web_search_enabled":
                reason = "web_search_enabled"
            elif spec.default_when_flag == "content_invitation_response_enabled":
                reason = "active_content_invitation"
            available.append(
                {
                    "name": spec.name,
                    "group": spec.group,
                    "reason": reason,
                    "schema": spec.schema,
                }
            )
            continue
        reason = "disabled"
        if spec.default_when_flag == "web_search_enabled":
            reason = "web_search_disabled"
        elif spec.default_when_flag == "content_invitation_response_enabled":
            reason = "no_active_content_invitation"
        disabled.append(
            {
                "name": spec.name,
                "group": spec.group,
                "reason": reason,
            }
        )

    first_round_tool_choice: Any = "none" if onboarding_active else _infer_proactive_update_tool_choice(text)
    return {
        "mode": "plain" if onboarding_active else "tools",
        "tools": tools,
        "available_tools": available,
        "disabled_tools": disabled,
        "available_tool_names": [_tool_name(schema) for schema in tools],
        "first_round_tool_choice": first_round_tool_choice,
        "max_tool_rounds": int(getattr(settings, "llm_max_tool_rounds", 3) or 3),
        "include_tool_instructions": bool(include_tool_instructions and not onboarding_active),
        "web_search_enabled": web_search_enabled,
        "content_invitation_response_enabled": content_invitation_enabled,
        "active_content_invitation_id": active_content_invitation.get("id") if active_content_invitation else None,
    }


def build_turn_llm_input(
    *,
    account_id: str,
    account: Dict[str, Any],
    session: Dict[str, Any],
    profile: Dict[str, Any],
    text: str,
    today: str,
    current_time: Optional[str] = None,
    onboarding_state: str = "",
    onboarding_active: bool,
    web_search_enabled: bool,
    force_web_search_enabled: Optional[bool] = None,
    onboarding_pre_written: Optional[Dict[str, Any]] = None,
    onboarding_pre_extracted: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
    include_tool_instructions: bool = True,
    debug_dry_run: bool = False,
    llm_provider: Optional[LLMProviderConfig] = None,
) -> Dict[str, Any]:
    """Build the exact LLM input envelope for a chat turn.

    This helper has no persistence side effects: callers that need onboarding
    pre-writes or message insertion must do that before invoking it.
    """
    _now = now or beijing_now()
    selected_llm_provider = llm_provider or resolve_active_llm_provider()
    current_session_id = int(session["id"])
    history_rows = list_recent_messages_for_account(
        account_id=account_id,
        limit=settings.llm_context_messages,
    )
    history = [
        {"role": row["role"], "content": row["content"]}
        for row in history_rows
    ]
    file_profile = read_user_profile(account_id)
    soul = extract_section(file_profile, "Soul")
    user_prefs = extract_section(file_profile, "User Preferences")
    long_term_memory = extract_section(file_profile, "Long-term Memory")
    agent_context = read_agent_context(
        account_id,
        display_name=account.get("display_name"),
    )
    suppress_carryover = _history_covers_previous_session(
        current_session_id=current_session_id,
        history_rows=history_rows,
    )
    carryover_summary = None if suppress_carryover else session.get("carryover_summary")
    history_metadata = _summarize_history_rows(history_rows)
    carryover_metadata = {
        "source_chars": len(session.get("carryover_summary") or ""),
        "included": bool(carryover_summary),
        "suppressed_by_history": suppress_carryover,
    }

    metadata: Dict[str, Any] = {
        "history_count": len(history),
        "history_cross_session": True,
        "history_session_count": history_metadata["session_count"],
        "history": history_metadata,
        "soul_chars": len(soul),
        "user_prefs_chars": len(user_prefs),
        "long_term_memory_chars": len(long_term_memory),
        "agent_context": agent_context.metadata(),
        "system_prompt_override": bool(profile.get("system_prompt")),
        "style": profile.get("style"),
        "display_name": account.get("display_name"),
        "onboarding_pre_written": onboarding_pre_written or {},
        "onboarding_active": onboarding_active,
        "onboarding_state": onboarding_state,
        "carryover_summary_included": bool(carryover_summary),
        "carryover_summary_suppressed_by_history": suppress_carryover,
        "carryover": carryover_metadata,
    }
    if debug_dry_run:
        metadata["debug_dry_run"] = True

    active_content_invitation = None
    if not onboarding_active and include_tool_instructions:
        active_content_invitation = get_active_content_invitation(
            account_id=account_id,
            now=_now.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
        )
        metadata["active_content_invitation_id"] = (
            active_content_invitation["id"] if active_content_invitation else None
        )
    tooling = _build_tooling_envelope(
        onboarding_active=onboarding_active,
        web_search_enabled=web_search_enabled,
        active_content_invitation=active_content_invitation,
        text=text,
        include_tool_instructions=include_tool_instructions,
    )

    onboarding_ctx = ""
    if onboarding_active:
        onboarding_ctx = build_onboarding_prompt_context(
            state=onboarding_state,
            user_name=_extract_user_name_from_context(agent_context.blocks),
            ai_name=_extract_ai_name_from_context(agent_context.blocks),
            persona=(onboarding_pre_written or {}).get("persona"),
            user_name_ask_count=_count_user_name_asks(session.get("turn_count", 0), onboarding_state),
            persona_ask_count=0 if onboarding_state != ONBOARDING_STEP3_SENT else 1,
            needs_confirmation=bool((onboarding_pre_extracted or {}).get("needs_confirmation")),
        )

    builder = PromptBuilder()
    build_result = builder.assemble(
        display_name=account.get("display_name"),
        soul=soul,
        user_prefs=user_prefs,
        long_term_memory=long_term_memory,
        daily_notes=None,
        carryover_summary=carryover_summary,
        system_prompt_override=profile.get("system_prompt"),
        style=profile.get("style"),
        agent_context=agent_context.blocks,
        onboarding_context=onboarding_ctx,
        today=today,
        current_time=current_time,
        weekday=beijing_weekday_str(_now),
        daypart=beijing_daypart_str(_now),
        model_name=selected_llm_provider.model,
        tool_instructions=(
            None if onboarding_active or not include_tool_instructions else _tool_instructions(
                active_content_invitation=active_content_invitation,
            )
        ),
    )
    system_prompt = build_result.prompt
    # 由 builder 自产元数据，替代历史写死的僵尸字段（今后若 wire daily notes 自动正确）。
    metadata["daily_notes_loaded"] = build_result.included("daily_notes")
    metadata["daily_notes_chars"] = build_result.final_chars("daily_notes")
    prompt_blocks = build_result.as_dict()
    metadata["prompt_blocks"] = prompt_blocks
    metadata["block_metrics"] = prompt_blocks
    metadata["tooling"] = {
        key: value
        for key, value in tooling.items()
        if key != "tools"
    }
    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    if debug_dry_run and text.strip():
        messages.append({"role": "user", "content": text.strip()})
        metadata["dry_run_user_text_included"] = True
    metadata["messages_count"] = len(messages)
    metadata["include_tool_instructions"] = bool(include_tool_instructions and not onboarding_active)
    metadata["web_search_enabled"] = web_search_enabled
    metadata["web_search_forced"] = force_web_search_enabled is not None

    return {
        "history": history,
        "system_prompt": system_prompt,
        "messages": messages,
        "metadata": metadata,
        "prompt_blocks": prompt_blocks,
        "history_metadata": history_metadata,
        "carryover": carryover_metadata,
        "tooling": tooling,
        "tools": tooling["tools"],
        "active_content_invitation": active_content_invitation,
        "agent_context": agent_context,
    }

def _turn_message_raw(
    *,
    source: str,
    identity,
    account_id: str,
    binding: dict,
    raw_payload: Optional[dict] = None,
    extra: Optional[dict] = None,
) -> dict:
    metadata = {
        "source": source,
        "channel": identity.channel,
        "channel_binding_id": binding.get("id"),
        "channel_account_id": identity.channel_account_id,
        "openclaw_session_key": identity.session_key,
        "account_active_session_key": ACCOUNT_ACTIVE_SESSION_KEY,
        "sender_id": identity.sender_id,
        "chat_id": identity.chat_id,
        "identity": identity_response_metadata(identity, account_id),
    }
    if raw_payload is not None:
        metadata["raw_payload"] = raw_payload
    if extra:
        metadata.update(extra)
    return metadata


def _send_tool_final_reply(
    *,
    identity,
    account_id: str,
    openclaw_session_key: str,
    reply: str,
    reply_message_id: str,
) -> Dict[str, Any]:
    """Send a tool-turn final reply out of band, bypassing OpenClaw's sync reply timeout."""
    to_user_id = (identity.chat_id or identity.sender_id or "").strip()
    if not to_user_id:
        raise ValueError("tool final reply target is empty")
    return node_gateway.node_send_text(
        node_id=resolve_node_for_account(account_id) or (settings.default_node_id or None),
        to_user_id=to_user_id,
        text=reply,
        gateway_timeout_ms=settings.openclaw_gateway_call_timeout_ms,
        account_id=identity.channel_account_id,
        session_key=openclaw_session_key,
        idempotency_key=f"tool-final-{account_id}-{reply_message_id}",
        channel=identity.channel,
    )


def _debug_trace_account_ids() -> set[str]:
    raw = getattr(settings, "debug_trace_account_ids", "") or ""
    return {item.strip() for item in raw.split(",") if item.strip()}


def _is_debug_trace_account(account_id: str) -> bool:
    return account_id in _debug_trace_account_ids()


def _get_nested_text(value: dict, *path: str) -> Optional[str]:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if current is None:
        return None
    text = str(current).strip()
    return text or None


def _openclaw_id_diagnostics(payload: OpenClawTurnRequest) -> dict:
    """Return non-sensitive upstream id candidates for turn dedupe debugging."""
    raw = payload.raw if isinstance(payload.raw, dict) else {}
    ctx = raw.get("ctx") if isinstance(raw.get("ctx"), dict) else {}
    event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
    ai4all_bridge = raw.get("ai4all_bridge") if isinstance(raw.get("ai4all_bridge"), dict) else {}
    account_candidates = (
        ai4all_bridge.get("account_candidates")
        if isinstance(ai4all_bridge.get("account_candidates"), dict)
        else {}
    )
    return {
        "payload_message_id": payload.message_id,
        "payload_event_id": payload.event_id,
        "ctx_run_id": _get_nested_text(raw, "ctx", "runId"),
        "ctx_session_id": _get_nested_text(raw, "ctx", "sessionId"),
        "ctx_message_id": _get_nested_text(raw, "ctx", "messageId"),
        "ctx_turn_id": _get_nested_text(raw, "ctx", "turnId"),
        "event_id": event.get("id"),
        "event_message_id": event.get("messageId") or event.get("message_id"),
        "event_msg_id": event.get("msgId") or event.get("msg_id"),
        "event_new_msg_id": event.get("newMsgId") or event.get("NewMsgId"),
        "bridge_session_id": account_candidates.get("sessionId"),
        "raw_keys": sorted(raw.keys()),
        "ctx_keys": sorted(ctx.keys()),
        "event_keys": sorted(event.keys()),
    }


@dataclass
class _TurnSetup:
    """阶段A（解析+守卫）的产物，下游 B/C/D 只读使用。"""
    account_id: str
    identity: Any
    account: Dict[str, Any]
    session: Dict[str, Any]
    binding: Dict[str, Any]
    profile: Dict[str, Any]
    sender_id: Optional[str]
    message_id: Optional[str]
    openclaw_session_key: str
    today: str
    business_day: str
    now: datetime
    profile_path: Any
    debug_trace_enabled: bool
    onboarding_state: str
    onboarding_active: bool
    llm_provider: LLMProviderConfig


@dataclass
class _InboundResult:
    """阶段B（入站持久化+筛查）的产物。inserted_id 必为非 None（去重已早返回）。"""
    text: str
    inserted_id: int
    image_described: bool
    image_understanding_failed: bool
    inbound_screen: Any
    inbound_blocked: bool


@dataclass
class _ReplyResult:
    """阶段C（解析回复）的产物。debug_metadata 为跨 C/D 有意累积的可变态。"""
    reply: str
    generation_error: Optional[str]
    normal_reply_generated: bool
    tool_names: List[str]
    onboarding_pre_extracted: Optional[dict]
    system_prompt: Optional[str]
    llm_messages: List[Any]
    debug_metadata: Dict[str, Any]
    llm_provider: Optional[LLMProviderConfig]


def _prepare_turn(
    payload: OpenClawTurnRequest,
    *,
    id_diagnostics: Dict[str, Any],
    started_at: float,
) -> Union[_TurnSetup, OpenClawTurnResponse]:
    """阶段A：身份/账号解析、unbound 收口、session/binding 初始化、onboarding welcome、
    disabled、限流、首次去重。命中守卫直接返回 OpenClawTurnResponse；否则返回 _TurnSetup。"""
    identity = resolve_openclaw_identity(
        channel=payload.channel,
        session_key=payload.session_key,
        channel_account_id=payload.channel_account_id or payload.account_id,
        sender_id=payload.sender_id,
        chat_id=payload.chat_id,
    )
    openclaw_session_key = identity.session_key
    resolved_account_id = resolve_account_id_for_inbound_channel_identity(
        channel=identity.channel,
        session_key=identity.session_key,
        channel_account_id=identity.channel_account_id,
    )
    if resolved_account_id is None:
        # 找不到 completed binding。收口：不再用 session_key 兜底创建账号，
        # 避免已解绑/未绑定的远端微信账号被当作新账号自动激活并继续回复。
        # 仅记 channel/account/session metadata，不记正文（解绑后隐私预期）。
        if getattr(settings, "openclaw_inbound_require_binding", True):
            logger.info(
                "openclaw_turn ignored unbound inbound channel=%s channel_account=%s "
                "session=%s message_id=%s",
                identity.channel,
                identity.channel_account_id,
                openclaw_session_key,
                payload.message_id or payload.event_id,
            )
            return OpenClawTurnResponse(
                status="ignored",
                no_reply=True,
                metadata={
                    "reason": "no_binding",
                    "channel": identity.channel,
                    "channel_account_id": identity.channel_account_id,
                    "session_key": openclaw_session_key,
                },
            )
        # 开关关闭（本地调试/测试）：保留 session_key 兜底。
        resolved_account_id = openclaw_session_key
    account_id = resolved_account_id
    sender_id = identity.sender_id
    message_id = payload.message_id or payload.event_id
    if not message_id:
        logger.warning(
            "openclaw_turn missing_message_id account=%s channel=%s channel_account=%s "
            "session=%s id_diagnostics=%s",
            account_id,
            identity.channel,
            identity.channel_account_id,
            openclaw_session_key,
            id_diagnostics,
        )

    now = beijing_now()
    today = now.date().isoformat()
    business_day = business_day_for(
        now,
        start_hour=int(
            getattr(settings, "conversation_session_business_day_start_hour", 4)
        ),
    )

    session_state = get_or_create_account_active_session_with_dreaming(
        account_id=account_id,
        channel=identity.channel,
        sender_id=sender_id,
        sender_name=payload.sender_name,
        chat_id=identity.chat_id,
        business_day=business_day,
        max_turns=int(getattr(settings, "conversation_session_max_turns", 500)),
    )
    binding = upsert_channel_binding(
        account_id=account_id,
        channel=identity.channel,
        session_key=openclaw_session_key,
        channel_account_id=identity.channel_account_id,
        sender_id=sender_id,
        chat_id=identity.chat_id,
        raw_identity=identity_response_metadata(identity, account_id),
    )
    account = session_state["account"]
    session = session_state["session"]
    profile_path = ensure_user_profile(account_id)
    ensure_agent_context_files(account_id, display_name=account.get("display_name"))
    ensure_account_state(account_id=account_id)
    debug_trace_enabled = _is_debug_trace_account(account_id)

    onboarding_state = get_account_onboarding_state(account_id=account_id)
    onboarding_channel_enabled = identity.channel == "openclaw-weixin"
    onboarding_active = onboarding_channel_enabled and is_onboarding_active(onboarding_state)

    # When the user's first inbound message arrives and onboarding hasn't started yet,
    # send the welcome proactively and absorb this message (no AI reply). The 5-second
    # timer at binding time always fails because the user peer isn't known until now.
    welcome_to_user_id = identity.chat_id or sender_id
    if onboarding_channel_enabled and onboarding_state == ONBOARDING_PENDING and welcome_to_user_id:
        try:
            # 多机:统一经 node_send_text 按归属节点即时发(本机直调/远程 push)。
            # 发送失败由下方 except 捕获 → fall through 正常处理本条消息(状态仍 pending,下条再触发)。
            node_gateway.node_send_text(
                node_id=resolve_node_for_account(account_id) or (settings.default_node_id or None),
                to_user_id=welcome_to_user_id,
                text=ONBOARDING_WELCOME_TEXT,
                gateway_timeout_ms=settings.openclaw_gateway_call_timeout_ms,
                account_id=identity.channel_account_id,
                # 固定幂等键:与 binding-timer welcome 共用,网关去重防并发重复欢迎。
                idempotency_key=f"onboarding-welcome-{account_id}",
                session_key=openclaw_session_key,
                channel=identity.channel,
            )
            set_account_onboarding_state(account_id=account_id, state=ONBOARDING_STEP1_SENT)
            logger.info(
                "onboarding welcome sent on first inbound message account=%s target=%s sender=%s chat=%s",
                account_id, welcome_to_user_id, sender_id, identity.chat_id,
            )
            latency_ms = int((time.monotonic() - started_at) * 1000)
            return OpenClawTurnResponse(
                status="ok",
                no_reply=True,
                metadata={
                    **identity_response_metadata(identity, account_id),
                    "latency_ms": latency_ms,
                    "onboarding_action": "welcome_sent_on_first_message",
                    "onboarding_welcome_to_user_id": welcome_to_user_id,
                },
            )
        except Exception as err:
            logger.warning(
                "onboarding welcome failed on first inbound message account=%s target=%s error=%s; "
                "falling through to normal turn",
                account_id, welcome_to_user_id, err,
            )

    if account.get("status") == "disabled":
        logger.info(
            "openclaw_turn ignored disabled account account=%s session=%s",
            account_id,
            openclaw_session_key,
        )
        return OpenClawTurnResponse(
            status="disabled",
            no_reply=True,
            metadata=identity_response_metadata(identity, account_id),
        )

    # 绑定有效且账号 active 后再记录正文，未绑定/已解绑/disabled 均已在上方返回。
    logger.info(
        "openclaw_turn text account=%s session=%s message_id=%s text=%r",
        account_id,
        openclaw_session_key,
        message_id,
        payload.text,
    )

    effective_rpm = settings.rate_limit_rpm if account.get("rpm_limit") is None else account["rpm_limit"]
    effective_daily = settings.rate_limit_daily if account.get("daily_limit") is None else account["daily_limit"]

    effective_rpm_window_seconds = max(
        float(getattr(settings, "rate_limit_rpm_window_seconds", 60.0) or 60.0),
        0.001,
    )

    if effective_rpm > 0 and not rate_limiter.check_rpm(
        account_id,
        effective_rpm,
        window_seconds=effective_rpm_window_seconds,
    ):
        logger.info(
            "openclaw_turn rpm_limited account=%s limit=%s window_seconds=%s message_id=%s "
            "id_diagnostics=%s",
            account_id,
            effective_rpm,
            effective_rpm_window_seconds,
            message_id,
            id_diagnostics,
        )
        return OpenClawTurnResponse(
            status="rate_limited",
            reply=settings.rate_limit_rpm_message,
            metadata={**identity_response_metadata(identity, account_id), "reason": "rpm"},
        )

    if effective_daily > 0:
        current_count = get_daily_usage(account_id=account_id, date=today)
        if current_count >= effective_daily:
            logger.info(
                "openclaw_turn daily_limited account=%s count=%s", account_id, current_count
            )
            return OpenClawTurnResponse(
                status="rate_limited",
                reply=settings.rate_limit_daily_message,
                metadata={
                    **identity_response_metadata(identity, account_id),
                    "reason": "daily",
                    "count": current_count,
                },
            )

    duplicate_reply = get_duplicate_reply(
        account_id=account_id,
        reply_to_message_id=message_id,
    )
    if duplicate_reply:
        latency_ms = int((time.monotonic() - started_at) * 1000)
        return OpenClawTurnResponse(
            status="duplicate",
            reply=duplicate_reply,
            metadata={**identity_response_metadata(identity, account_id), "latency_ms": latency_ms},
        )

    return _TurnSetup(
        account_id=account_id,
        identity=identity,
        account=account,
        session=session,
        binding=binding,
        profile=session_state.get("profile") or {},
        sender_id=sender_id,
        message_id=message_id,
        openclaw_session_key=openclaw_session_key,
        today=today,
        business_day=business_day,
        now=now,
        profile_path=profile_path,
        debug_trace_enabled=debug_trace_enabled,
        onboarding_state=onboarding_state,
        onboarding_active=onboarding_active,
        llm_provider=resolve_active_llm_provider(),
    )


def _persist_and_screen_inbound(
    payload: OpenClawTurnRequest,
    setup: _TurnSetup,
    *,
    started_at: float,
    timings: Dict[str, int],
) -> Union[_InboundResult, OpenClawTurnResponse]:
    """阶段B：text 规范化+图片理解、插入入站消息(+插入后去重早返回)、入站审核 screen、
    referral、图片计费。返回 _InboundResult；插入后去重命中则返回 OpenClawTurnResponse。"""
    account_id = setup.account_id
    identity = setup.identity
    binding = setup.binding
    session = setup.session
    message_id = setup.message_id
    today = setup.today

    text = (payload.text or "").strip()
    # 图片轮：调 VL 产出多维描述，合成进 user 历史（支撑图后追问 C 场景），
    # 再走主链路按人设接话；VL 失败/总开关关闭则走兜底，跳过主模型（红线：不瞎猜）。
    image_described = False
    image_understanding_failed = False
    if payload.message_type == "image":
        image_started = time.monotonic()
        caption = text
        description = None
        if settings.image_understanding_enabled:
            media = payload.media
            if media is not None and (media.data_base64 or media.path or media.url):
                # 来源优先级（多机字节 > 单机本地路径 > 远程 URL）在 describe_image 内部统一。
                description = describe_image(
                    image_b64=media.data_base64,
                    image_format=media.format,
                    image_path=media.path,
                    image_url=media.url,
                    caption=caption,
                )
        if description:
            text = f"{caption}\n[用户发来一张图片：{description}]".strip()
            image_described = True
        else:
            image_understanding_failed = True
            text = caption or "[图片]"
        _record_timing(timings, "image_understanding_ms", image_started)
    elif not text and payload.message_type == "voice":
        text = "[voice message]"

    inbound_db_started = time.monotonic()
    with db_connect() as conn:
        inserted_id = insert_message(
            account_id=account_id,
            session_id=session["id"],
            message_id=message_id,
            reply_to_message_id=None,
            direction="inbound",
            role="user",
            message_type=payload.message_type,
            content=text,
            raw=_turn_message_raw(
                source="openclaw_turn",
                identity=identity,
                account_id=account_id,
                binding=binding,
                raw_payload=payload.raw,
                extra={
                    "message_id": message_id,
                    "message_type": payload.message_type,
                },
            ),
            conn=conn,
        )
        if inserted_id is not None:
            increment_daily_usage(account_id=account_id, date=today, conn=conn)
    _record_timing(timings, "inbound_db_ms", inbound_db_started)
    if inserted_id is None:
        duplicate_reply = get_duplicate_reply(
            account_id=account_id,
            reply_to_message_id=message_id,
        )
        latency_ms = _elapsed_ms(started_at)
        return OpenClawTurnResponse(
            status="duplicate",
            reply=duplicate_reply or "刚刚这条消息我已经收到啦。",
            metadata={**identity_response_metadata(identity, account_id), "latency_ms": latency_ms},
        )

    # 入站内容同步筛查（阿里云云审核为主 + 本地红线补充）。命中即停止本轮回复并进入人工队列。
    # 阿里云未开启时内部回退第一阶段异步审核并放行；筛查自身异常时 fail-open（放行本轮回复）。
    inbound_screen = None
    inbound_moderation_started = time.monotonic()
    try:
        inbound_content_kind = "voice_transcript" if payload.message_type == "voice" else payload.message_type
        inbound_screen = screen_inbound_message_sync(
            message_db_id=int(inserted_id),
            account_id=account_id,
            session_id=int(session["id"]),
            content_kind=inbound_content_kind,
            text=text,
            media=payload.media,
            source_message_id=message_id,
            metadata={
                "message_type": payload.message_type,
                "image_described": image_described,
                "image_understanding_failed": image_understanding_failed,
            },
        )
    except Exception as err:
        logger.exception(
            "inbound moderation screen failed account=%s message_db_id=%s error=%s",
            account_id,
            inserted_id,
            err,
        )
        inbound_screen = None
    finally:
        _record_timing(timings, "inbound_moderation_ms", inbound_moderation_started)
    inbound_blocked = bool(inbound_screen is not None and not inbound_screen.allowed)
    if inbound_blocked:
        # 命中风险的入站原文打审核标记：从后续所有 LLM 上下文/记忆/turn 计数中剔除，避免下一轮被重新喂给模型。
        try:
            mark_message_moderation_blocked(
                message_db_id=int(inserted_id),
                account_id=account_id,
            )
        except Exception as err:
            logger.exception(
                "mark inbound moderation blocked failed account=%s message_db_id=%s error=%s",
                account_id,
                inserted_id,
                err,
            )

    referral_started = time.monotonic()
    try:
        process_referral_message_for_account(
            account_id=account_id,
            message_db_id=int(inserted_id),
        )
    except Exception as err:
        logger.exception(
            "referral message processing failed account=%s message_db_id=%s error=%s",
            account_id,
            inserted_id,
            err,
        )
    finally:
        _record_timing(timings, "referral_ms", referral_started)

    # VL 成功后记一次独立的图片理解成本事件（固定贝壳，带总开关，与 chat 扣费相互独立）。
    if image_described:
        image_charge_started = time.monotonic()
        try:
            record_image_understanding_charge(
                account_id=account_id,
                source_id=message_id or str(inserted_id),
                idempotency_key=f"image-understanding-{account_id}-{message_id or inserted_id}",
                model=settings.image_understanding_model,
                metadata={
                    "message_id": message_id,
                    "session_id": int(session["id"]),
                },
            )
        except Exception as err:
            logger.exception(
                "image understanding charge failed account=%s message_id=%s error=%s",
                account_id,
                message_id,
                err,
            )
        finally:
            _record_timing(timings, "image_charge_ms", image_charge_started)

    return _InboundResult(
        text=text,
        inserted_id=int(inserted_id),
        image_described=image_described,
        image_understanding_failed=image_understanding_failed,
        inbound_screen=inbound_screen,
        inbound_blocked=inbound_blocked,
    )


def _resolve_turn_reply(
    payload: OpenClawTurnRequest,
    setup: _TurnSetup,
    inbound: _InboundResult,
    *,
    background_loop: Optional[asyncio.AbstractEventLoop],
    force_web_search_enabled: Optional[bool],
    timings: Dict[str, int],
) -> _ReplyResult:
    """阶段C：决定本轮回复来源（inbound_blocked / #重置 / #状态 / 图片失败 / 正常聊天）。
    正常分支内做 onboarding 预抽取、build_turn_llm_input、构建 TurnContext 并调 LLM。"""
    account_id = setup.account_id
    account = setup.account
    session = setup.session
    binding = setup.binding
    identity = setup.identity
    sender_id = setup.sender_id
    message_id = setup.message_id
    openclaw_session_key = setup.openclaw_session_key
    today = setup.today
    business_day = setup.business_day
    now = setup.now
    profile = setup.profile
    profile_path = setup.profile_path
    debug_trace_enabled = setup.debug_trace_enabled
    onboarding_state = setup.onboarding_state
    onboarding_active = setup.onboarding_active
    llm_provider = setup.llm_provider
    text = inbound.text
    inbound_blocked = inbound.inbound_blocked
    image_understanding_failed = inbound.image_understanding_failed

    generation_error = None
    normal_reply_generated = False
    tool_names_used: List[str] = []
    onboarding_pre_extracted = None
    system_prompt = None
    llm_messages = []
    web_search_enabled_for_turn = (
        bool(force_web_search_enabled)
        if force_web_search_enabled is not None
        else bool(getattr(settings, "web_search_enabled", False))
    )
    debug_metadata = {
        "trace_kind": "ai4all_turn",
        "debug_trace_enabled": debug_trace_enabled,
        "web_search_enabled": web_search_enabled_for_turn,
        "web_search_forced": force_web_search_enabled is not None,
        "identity": identity_response_metadata(identity, account_id),
        "channel_binding_id": binding["id"],
        "channel": identity.channel,
        "session_key": openclaw_session_key,
        "openclaw_session_key": openclaw_session_key,
        "account_active_session_key": ACCOUNT_ACTIVE_SESSION_KEY,
        "sender_id": sender_id,
        "message_type": payload.message_type,
        "today": today,
        "business_day": business_day,
        "session_business_day": session.get("business_day"),
        "session_turn_count": session.get("turn_count"),
        "session_carryover_chars": len(session.get("carryover_summary") or ""),
    }
    if inbound_blocked:
        # 入站命中风险：不调用主模型，返回固定安全话术；本轮不计费、不推进 onboarding。
        reply = str(
            getattr(settings, "moderation_inbound_blocked_reply_text", "")
            or "这个话题我不太方便继续，我们换个轻松点的聊聊吧～"
        )
    elif text == "#重置会话":
        clear_session_messages(session_id=session["id"])
        reply = "已重置当前会话。"
    elif text == "#状态":
        reply = (
            f"当前会话正常。account_id={account_id}, "
            f"active_session_key={ACCOUNT_ACTIVE_SESSION_KEY}, "
            f"openclaw_session_key={openclaw_session_key}"
        )
    elif image_understanding_failed:
        # 图片没看清/未开启理解：走兜底话术，不调主模型（禁止无描述瞎猜）。
        reply = settings.image_understanding_fallback_text
    else:
        try:
            prompt_started = time.monotonic()
            agent_context = read_agent_context(
                account_id,
                display_name=account.get("display_name"),
            )
            onboarding_pre_written = {}
            if onboarding_active and onboarding_state in {ONBOARDING_STEP1_SENT, ONBOARDING_STEP2_SENT, ONBOARDING_STEP3_SENT}:
                onboarding_pre_extracted = _extract_onboarding_info_sync(
                    user_text=text,
                    current_state=onboarding_state,
                )
                # Writing before prompt build ensures the AI sees what was just collected.
                _has_onboarding_write = any(
                    onboarding_pre_extracted.get(key)
                    for key in ("user_name", "ai_name", "persona", "persona_custom")
                )
                if _has_onboarding_write:
                    onboarding_pre_written = apply_extracted_onboarding_info(
                        account_id=account_id,
                        extracted=onboarding_pre_extracted,
                        current_state=onboarding_state,
                    )
                    if onboarding_pre_written:
                        agent_context = read_agent_context(
                            account_id,
                            display_name=account.get("display_name"),
                        )
            llm_input = build_turn_llm_input(
                account_id=account_id,
                account=account,
                session=session,
                profile=profile,
                text=text,
                today=today,
                current_time=now.strftime("%H:%M"),
                onboarding_state=onboarding_state,
                onboarding_active=onboarding_active,
                onboarding_pre_written=onboarding_pre_written,
                onboarding_pre_extracted=onboarding_pre_extracted,
                web_search_enabled=web_search_enabled_for_turn,
                force_web_search_enabled=force_web_search_enabled,
                now=now,
                llm_provider=llm_provider,
            )
            history = llm_input["history"]
            system_prompt = llm_input["system_prompt"]
            llm_messages = llm_input["messages"]
            tooling = llm_input["tooling"]
            debug_metadata.update(llm_input["metadata"])

            ctx = TurnContext(
                account_id=account_id,
                account=account,
                session=session,
                identity=identity,
                binding=binding,
                message_id=message_id,
                text=text,
                today=today,
                business_day=business_day,
                profile_path=profile_path,
                debug_trace_enabled=debug_trace_enabled,
                onboarding_state=onboarding_state,
                onboarding_active=onboarding_active,
                recent_messages=history,
                background_loop=background_loop,
                web_search_enabled=web_search_enabled_for_turn,
            )
            _record_timing(timings, "prompt_build_ms", prompt_started)

            generation_started = time.monotonic()
            if onboarding_active:
                try:
                    reply = generate_reply(
                        user_text=text,
                        history=history,
                        system_prompt=system_prompt,
                        messages=llm_messages,
                        provider=llm_provider,
                    )
                finally:
                    _record_timing(timings, "reply_generation_ms", generation_started)
                if onboarding_state == ONBOARDING_PENDING:
                    reply = _ensure_pending_onboarding_question(reply)
            else:
                tool_thinking_sender = _make_tool_thinking_sender(
                    identity=identity,
                    account_id=account_id,
                    openclaw_session_key=openclaw_session_key,
                )

                def _on_tool_detected(tool_names: List[str]) -> None:
                    cleaned = [
                        str(name).strip()
                        for name in (tool_names or [])
                        if str(name or "").strip()
                    ]
                    if cleaned:
                        tool_names_used.extend(cleaned)
                    if tool_thinking_sender is not None:
                        tool_thinking_sender(tool_names)

                try:
                    reply, generation_error = generate_reply_with_tools(
                        user_text=text,
                        history=history,
                        system_prompt=system_prompt,
                        tools=llm_input["tools"],
                        ctx=ctx,
                        first_round_tool_choice=tooling["first_round_tool_choice"],
                        messages=llm_messages,
                        provider=llm_provider,
                        on_tool_detected=_on_tool_detected,
                    )
                finally:
                    _record_timing(timings, "reply_generation_ms", generation_started)
            if generation_error and not reply:
                reply = _GENERATION_ERROR_REPLY
                # 用户将真实收到「卡住了」兜底回复(生成失败且无可用回复)。ERROR 级 → 经 ai4all
                # 命名空间的 Feishu handler 推送 FEISHU_ALERT_WEBHOOK_URL(同签名 5min 冷却+脱敏)。
                # 这里补的是 generate_reply_with_tools 返回 error 字符串的静默路径(超时/空响应/
                # 限流/工具轮超限等);LLM 直接抛异常的路径已由下方 except 的 logger.exception 覆盖。
                logger.error(
                    "user received generation fallback reply account=%s generation_error=%s",
                    account_id,
                    generation_error,
                )
            normal_reply_generated = generation_error is None
            if tool_names_used:
                debug_metadata["tool_names_used"] = tool_names_used
        except Exception as err:
            logger.exception("reply generation failed: %s", err)
            generation_error = str(err)
            reply = _GENERATION_ERROR_REPLY

    return _ReplyResult(
        reply=reply,
        generation_error=generation_error,
        normal_reply_generated=normal_reply_generated,
        tool_names=tool_names_used,
        onboarding_pre_extracted=onboarding_pre_extracted,
        system_prompt=system_prompt,
        llm_messages=llm_messages,
        debug_metadata=debug_metadata,
        llm_provider=llm_provider,
    )


def _finalize_turn(
    payload: OpenClawTurnRequest,
    setup: _TurnSetup,
    inbound: _InboundResult,
    result: _ReplyResult,
    *,
    latency_ms: int,
    background_loop: Optional[asyncio.AbstractEventLoop],
    timings: Dict[str, int],
) -> OpenClawTurnResponse:
    """阶段D：出站同步审核守卫（就地兜底覆写 reply）、debug trace、出站持久化+turn_count、
    出站审核入队、计费、onboarding 状态推进、after-turn 派发、构建响应。"""
    account_id = setup.account_id
    identity = setup.identity
    session = setup.session
    binding = setup.binding
    message_id = setup.message_id
    openclaw_session_key = setup.openclaw_session_key
    business_day = setup.business_day
    profile_path = setup.profile_path
    debug_trace_enabled = setup.debug_trace_enabled
    onboarding_active = setup.onboarding_active
    onboarding_state = setup.onboarding_state
    inserted_id = inbound.inserted_id
    inbound_blocked = inbound.inbound_blocked
    inbound_screen = inbound.inbound_screen
    text = inbound.text
    reply = result.reply
    generation_error = result.generation_error
    normal_reply_generated = result.normal_reply_generated
    tool_names = result.tool_names
    onboarding_pre_extracted = result.onboarding_pre_extracted
    system_prompt = result.system_prompt
    llm_messages = result.llm_messages
    debug_metadata = result.debug_metadata
    active_llm_provider = (
        result.llm_provider.redacted()
        if result.llm_provider is not None
        else resolve_active_llm_provider().redacted()
    )
    active_llm_model = str(active_llm_provider.get("model") or "")
    debug_metadata.setdefault("llm_provider_id", active_llm_provider.get("id"))
    debug_metadata.setdefault("llm_protocol", active_llm_provider.get("protocol"))

    outbound_guard_started = time.monotonic()
    reply_message_id = f"reply-{uuid.uuid4()}"
    moderation_reply_metadata: Dict[str, Any] = {}
    sync_decision = check_sync_guard(
        account_id=account_id,
        text=reply,
        direction="outbound",
        content_kind="text",
        source_type="generated_reply",
        source_id=reply_message_id,
    )
    if not sync_decision.allowed:
        original_reply = reply
        try:
            blocked_task = create_sync_block_task(
                account_id=account_id,
                session_id=int(session["id"]),
                source_type="generated_reply",
                source_id=reply_message_id,
                direction="outbound",
                content_kind="text",
                text=original_reply,
                decision=sync_decision,
                metadata={
                    "reply_to_message_id": message_id,
                    "message_db_id": inserted_id,
                },
            )
        except Exception as err:
            logger.exception(
                "sync moderation block task failed account=%s reply_message_id=%s error=%s",
                account_id,
                reply_message_id,
                err,
            )
            blocked_task = None
        reply = str(getattr(settings, "moderation_safe_fallback_text", "") or "这条内容我不能继续发送，我们换个安全的话题吧。")
        moderation_reply_metadata = {
            "moderation_blocked": True,
            "moderation_task_id": blocked_task.get("id") if blocked_task else None,
            "moderation_risk_level": sync_decision.level,
            "moderation_categories": sync_decision.categories,
        }
        debug_metadata.update(moderation_reply_metadata)

    # 入站被拦截：在出站消息上标注入站审核信息，便于排查与审计（任务已由同步筛查创建）。
    if inbound_blocked and inbound_screen is not None:
        moderation_reply_metadata.update(
            {
                "moderation_inbound_blocked": True,
                "moderation_task_id": inbound_screen.task_id,
                "moderation_risk_level": inbound_screen.level,
                "moderation_categories": inbound_screen.categories,
                "moderation_degraded": inbound_screen.degraded,
            }
        )
        debug_metadata.update(moderation_reply_metadata)
    _record_timing(timings, "outbound_sync_guard_ms", outbound_guard_started)

    trace_id = None
    outbound_inserted_id = None
    outbound_db_started = time.monotonic()
    with db_connect() as conn:
        if debug_trace_enabled:
            trace_id = f"trace-{uuid.uuid4()}"
            insert_debug_trace(
                trace_id=trace_id,
                account_id=account_id,
                session_id=session["id"],
                message_id=message_id,
                source="ai4all",
                llm_model=active_llm_model,
                system_prompt=system_prompt,
                messages=llm_messages,
                reply=reply,
                metadata=debug_metadata,
                latency_ms=latency_ms,
                error=generation_error,
                conn=conn,
            )

        outbound_raw_extra = {
            "message_id": reply_message_id,
            "reply_to_message_id": message_id,
        }
        if tool_names:
            outbound_raw_extra["tool_names_used"] = tool_names
            outbound_raw_extra["delivery_mode"] = "out_of_band_tool_final"
        outbound_raw_extra.update(moderation_reply_metadata)
        outbound_inserted_id = insert_message(
            account_id=account_id,
            session_id=session["id"],
            message_id=reply_message_id,
            reply_to_message_id=message_id,
            direction="outbound",
            role="assistant",
            message_type="text",
            content=reply,
            raw=_turn_message_raw(
                source="ai4all_sync_reply",
                identity=identity,
                account_id=account_id,
                binding=binding,
                extra=outbound_raw_extra,
            ),
            latency_ms=latency_ms,
            error=generation_error,
            conn=conn,
        )

        if not generation_error and not inbound_blocked and text and text not in _SPECIAL_COMMANDS:
            increment_session_turn_count(session_id=int(session["id"]), conn=conn)
    _record_timing(timings, "outbound_db_ms", outbound_db_started)

    if debug_trace_enabled:
        logger.info(
            "debug trace recorded trace_id=%s account=%s session=%s message_id=%s messages=%s prompt_chars=%s",
            trace_id,
            account_id,
            openclaw_session_key,
            message_id,
            len(llm_messages),
            len(system_prompt or ""),
        )

    # 已被同步红线拦截的回复，原文已由 create_sync_block_task 记录成审核任务；
    # 此时 reply 只是安全兜底文案，无需再为它创建一条 machine_passed 任务，避免队列里同一条回复出现两个 task。
    if (
        outbound_inserted_id is not None
        and not moderation_reply_metadata.get("moderation_blocked")
        and not inbound_blocked
    ):
        outbound_moderation_started = time.monotonic()
        try:
            enqueue_message_for_moderation(
                message_db_id=int(outbound_inserted_id),
                account_id=account_id,
                session_id=int(session["id"]),
                direction="outbound",
                content_kind="text",
                text=reply,
                media=None,
                source_message_id=reply_message_id,
                metadata={
                    "reply_to_message_id": message_id,
                },
            )
        except Exception as err:
            logger.exception(
                "outbound moderation enqueue failed account=%s message_db_id=%s error=%s",
                account_id,
                outbound_inserted_id,
                err,
            )
        finally:
            _record_timing(timings, "outbound_moderation_enqueue_ms", outbound_moderation_started)

    billing_result = None
    if not generation_error and normal_reply_generated and text and text not in _SPECIAL_COMMANDS:
        billing_started = time.monotonic()
        try:
            billing_result = record_chat_usage_charge(
                account_id=account_id,
                model=active_llm_model,
                messages=llm_messages,
                reply=reply,
                source_type="chat_turn",
                source_id=reply_message_id,
                idempotency_key=f"chat-turn-{account_id}-{message_id or inserted_id}",
                metadata={
                    "message_id": message_id,
                    "reply_message_id": reply_message_id,
                    "session_id": int(session["id"]),
                    "estimated": True,
                },
            )
        except Exception as err:
            logger.exception("chat usage charge failed account=%s reply=%s error=%s", account_id, reply_message_id, err)
        finally:
            _record_timing(timings, "billing_ms", billing_started)

    # Advance onboarding state synchronously after reply so onboarding completion
    # does not depend on the after-turn background loop.
    if not generation_error and normal_reply_generated and onboarding_active:
        onboarding_advance_started = time.monotonic()
        if onboarding_state == "pending":
            try:
                set_account_onboarding_state(account_id=account_id, state=ONBOARDING_STEP1_SENT)
                logger.info("onboarding state advanced account=%s pending -> step1_sent", account_id)
            except Exception as err:
                logger.error("onboarding state set failed account=%s error=%s", account_id, err)
        elif onboarding_pre_extracted is not None:
            try:
                # session.turn_count is pre-increment; normal flow reaches step2 at count=2.
                # Any higher count means we've already held for one confirmation turn.
                _confirmation_ask_count = (
                    max(0, int(session.get("turn_count", 0)) - 2)
                    if onboarding_state == ONBOARDING_STEP2_SENT
                    else 0
                )
                new_state = next_onboarding_state(
                    current_state=onboarding_state,
                    extracted=onboarding_pre_extracted,
                    user_name_ask_count=0,
                    persona_ask_count=0,
                    confirmation_ask_count=_confirmation_ask_count,
                )
                if new_state != onboarding_state:
                    set_account_onboarding_state(account_id=account_id, state=new_state)
                    logger.info(
                        "onboarding state advanced account=%s %s -> %s",
                        account_id,
                        onboarding_state,
                        new_state,
                    )
            except Exception as err:
                logger.exception("onboarding state advance failed account=%s error=%s", account_id, err)
        _record_timing(timings, "onboarding_advance_ms", onboarding_advance_started)

    should_run_after_turn = (
        not generation_error
        and not inbound_blocked
        and text
        and text not in _SPECIAL_COMMANDS
    )
    after_turn_enqueue_started = time.monotonic()
    if should_run_after_turn and background_loop is None:
        # 不再静默吞掉：无后台事件循环时（独立进程/脚本/测试显式 None）after-turn
        # 记忆写入与 commitment 抽取会被跳过，至少记一条 warning 让数据丢失可观测。
        logger.warning(
            "after-turn work skipped: no background loop "
            "(daily memory + commitment extraction not run) account=%s",
            account_id,
        )
    if should_run_after_turn and background_loop is not None:
        turns_for_memory = [
            {"role": "user", "content": text},
            {"role": "assistant", "content": reply},
        ]
        background_loop.call_soon_threadsafe(
            background_loop.create_task,
            write_memory(
                account_id=account_id,
                turns=turns_for_memory,
                today=business_day,
                session_id=int(session["id"]),
                user_message_id=message_id,
                assistant_message_id=reply_message_id,
                modality=payload.message_type,
                extra_metadata={
                    "channel": identity.channel,
                    "channel_binding_id": binding["id"],
                    "openclaw_session_key": openclaw_session_key,
                    "account_active_session_key": ACCOUNT_ACTIVE_SESSION_KEY,
                },
            ),
        )
        # turn 后确定性关系状态更新（30 条阈值 + 资源风险）。不阻塞主回复，
        # 失败在函数内部记日志；inbound 已在此前持久化，计数含当前消息。
        background_loop.call_soon_threadsafe(
            background_loop.create_task,
            asyncio.to_thread(
                maybe_update_relationship_state_after_turn,
                account_id=account_id,
            ),
        )
        if normal_reply_generated:
            background_loop.call_soon_threadsafe(
                background_loop.create_task,
                asyncio.to_thread(
                    extract_commitment_from_turn,
                    account_id=account_id,
                    session_id=int(session["id"]),
                    user_text=text,
                    assistant_text=reply,
                    source_message_id=message_id,
                    source_reply_message_id=reply_message_id,
                ),
            )
    if should_run_after_turn:
        _record_timing(timings, "after_turn_enqueue_ms", after_turn_enqueue_started)

    response_metadata = {
        **identity_response_metadata(identity, account_id),
        "channel_binding_id": binding["id"],
        "account_active_session_key": ACCOUNT_ACTIVE_SESSION_KEY,
        "message_type": payload.message_type,
        "latency_ms": latency_ms,
        "user_profile_path": str(profile_path),
        "debug_trace_id": trace_id,
        "billing": {
            "charged": bool(billing_result and billing_result.get("ledger")),
            "estimated": True,
            "amount_shells": (
                billing_result["ledger"]["amount_shells"]
                if billing_result and billing_result.get("ledger")
                else None
            ),
            "balance_shells": (
                billing_result["wallet"]["balance_shells"]
                if billing_result and billing_result.get("wallet")
                else None
            ),
        },
    }
    if tool_names:
        response_metadata["tool_names_used"] = tool_names
        try:
            send_result = _send_tool_final_reply(
                identity=identity,
                account_id=account_id,
                openclaw_session_key=openclaw_session_key,
                reply=reply,
                reply_message_id=reply_message_id,
            )
            response_metadata["delivery_mode"] = "out_of_band_tool_final"
            response_metadata["gateway_message_id"] = (
                send_result.get("messageId") or send_result.get("message_id")
            )
            logger.info(
                "tool_final_reply_sent account=%s tools=%s reply_message_id=%s gateway_message_id=%s",
                account_id,
                tool_names,
                reply_message_id,
                response_metadata["gateway_message_id"],
            )
            return OpenClawTurnResponse(
                status="ok",
                no_reply=True,
                metadata=response_metadata,
            )
        except Exception as err:
            response_metadata["delivery_mode"] = "sync_response_fallback"
            response_metadata["tool_final_send_error"] = str(err)
            logger.warning(
                "tool_final_reply send failed account=%s tools=%s reply_message_id=%s error=%s",
                account_id,
                tool_names,
                reply_message_id,
                err,
            )

    return OpenClawTurnResponse(
        status="ok",
        reply=reply,
        metadata=response_metadata,
    )


def handle_openclaw_turn(
    payload: OpenClawTurnRequest,
    *,
    background_loop: Optional[asyncio.AbstractEventLoop] = None,
    force_web_search_enabled: Optional[bool] = None,
) -> OpenClawTurnResponse:
    """每条入站微信消息的主入口。编排脊柱：解析+守卫 → 入站持久化+筛查 →
    解析回复 → 终结。各阶段细节见对应 _prepare_turn/_persist_and_screen_inbound/
    _resolve_turn_reply/_finalize_turn。"""
    started_at = time.monotonic()
    timings: Dict[str, int] = {}
    id_diagnostics = _openclaw_id_diagnostics(payload)
    # 入口只记 metadata，不记正文：未绑定/已解绑入站会在下方收口返回，
    # 正文日志移到绑定校验通过后（见 disabled 检查之后）。
    logger.info(
        "openclaw_turn received channel=%s session=%s sender=%s type=%s "
        "message_id=%s event_id=%s ctx_run_id=%s ctx_session_id=%s raw_keys=%s",
        payload.channel,
        payload.session_key,
        payload.sender_id,
        payload.message_type,
        payload.message_id,
        payload.event_id,
        id_diagnostics.get("ctx_run_id"),
        id_diagnostics.get("ctx_session_id"),
        id_diagnostics.get("raw_keys"),
    )

    if payload.chat_type != "private":
        timings["reply_ready_ms"] = _elapsed_ms(started_at)
        _log_turn_timing(
            payload=payload,
            timings=timings,
            started_at=started_at,
            status="ignored_non_private",
        )
        return OpenClawTurnResponse(status="ignored", no_reply=True)

    prepare_started = time.monotonic()
    setup = _prepare_turn(payload, id_diagnostics=id_diagnostics, started_at=started_at)
    _record_timing(timings, "prepare_ms", prepare_started)
    if isinstance(setup, OpenClawTurnResponse):
        timings["reply_ready_ms"] = _elapsed_ms(started_at)
        setup_metadata = setup.metadata or {}
        _log_turn_timing(
            payload=payload,
            timings=timings,
            started_at=started_at,
            status=setup.status,
            account_id=setup_metadata.get("account_id") or setup_metadata.get("ai4all_account_id"),
            session=setup_metadata.get("session_key") or payload.session_key,
            message_id=payload.message_id or payload.event_id,
        )
        return setup

    inbound_started = time.monotonic()
    inbound = _persist_and_screen_inbound(
        payload,
        setup,
        started_at=started_at,
        timings=timings,
    )
    _record_timing(timings, "inbound_total_ms", inbound_started)
    if isinstance(inbound, OpenClawTurnResponse):
        timings["reply_ready_ms"] = _elapsed_ms(started_at)
        _log_turn_timing(
            payload=payload,
            timings=timings,
            started_at=started_at,
            status=inbound.status,
            account_id=setup.account_id,
            session=setup.openclaw_session_key,
            message_id=setup.message_id,
        )
        return inbound

    reply_started = time.monotonic()
    result = _resolve_turn_reply(
        payload,
        setup,
        inbound,
        background_loop=background_loop,
        force_web_search_enabled=force_web_search_enabled,
        timings=timings,
    )
    _record_timing(timings, "reply_total_ms", reply_started)

    latency_ms = _elapsed_ms(started_at)
    timings["reply_ready_ms"] = latency_ms

    finalize_started = time.monotonic()
    response = _finalize_turn(
        payload,
        setup,
        inbound,
        result,
        latency_ms=latency_ms,
        background_loop=background_loop,
        timings=timings,
    )
    _record_timing(timings, "finalize_total_ms", finalize_started)
    _log_turn_timing(
        payload=payload,
        timings=timings,
        started_at=started_at,
        status=response.status,
        account_id=setup.account_id,
        session=setup.openclaw_session_key,
        message_id=setup.message_id,
        error=result.generation_error,
    )
    return response
