import asyncio
import logging
import time
import uuid
from datetime import datetime

from app.time_utils import beijing_now, beijing_daypart_str, beijing_weekday_str
from typing import Any, Dict, List, Optional

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
    set_account_onboarding_state,
    should_inline_dispatch_for_account,
    upsert_channel_binding,
)
from app.identity import identity_response_metadata, resolve_openclaw_identity
from app.image_understanding import describe_image
from app.llm import generate_reply, generate_reply_with_tools
from app.memory_writer import write_memory
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
from app.tools import get_default_tools
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
from app.openclaw_gateway import send_weixin_text
from app.proactive.messaging import enqueue_onboarding_welcome
from app.user_profiles import (
    ensure_agent_context_files,
    ensure_user_profile,
    read_agent_context,
    read_user_profile,
)


logger = logging.getLogger("ai4all.turn_service")

_SPECIAL_COMMANDS = {"#重置会话", "#状态"}


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
) -> Dict[str, Any]:
    """Build the exact LLM input envelope for a chat turn.

    This helper has no persistence side effects: callers that need onboarding
    pre-writes or message insertion must do that before invoking it.
    """
    _now = now or beijing_now()
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

    metadata: Dict[str, Any] = {
        "history_count": len(history),
        "history_cross_session": True,
        "history_session_count": len({int(row["session_id"]) for row in history_rows if row.get("session_id") is not None}),
        "soul_chars": len(soul),
        "user_prefs_chars": len(user_prefs),
        "long_term_memory_chars": len(long_term_memory),
        "daily_notes_loaded": False,
        "daily_notes_chars": 0,
        "agent_context": agent_context.metadata(),
        "system_prompt_override": bool(profile.get("system_prompt")),
        "style": profile.get("style"),
        "display_name": account.get("display_name"),
        "onboarding_pre_written": onboarding_pre_written or {},
        "onboarding_active": onboarding_active,
        "onboarding_state": onboarding_state,
        "carryover_summary_included": bool(carryover_summary),
        "carryover_summary_suppressed_by_history": suppress_carryover,
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
    system_prompt = builder.build(
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
        model_name=settings.llm_model,
        tool_instructions=(
            None if onboarding_active or not include_tool_instructions else _tool_instructions(
                active_content_invitation=active_content_invitation,
            )
        ),
    )
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


def handle_openclaw_turn(
    payload: OpenClawTurnRequest,
    *,
    background_loop: Optional[asyncio.AbstractEventLoop] = None,
    force_web_search_enabled: Optional[bool] = None,
) -> OpenClawTurnResponse:
    started_at = time.monotonic()
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
        return OpenClawTurnResponse(status="ignored", no_reply=True)

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
            # 多机:本机账号 inline 直发(standalone/本机归属);远程账号或 central 非 inline
            # 入队由归属节点发(turn 在中心跑,中心不持有远程会话,不能直接 send)。
            if should_inline_dispatch_for_account(account_id, settings):
                send_weixin_text(
                    to_user_id=welcome_to_user_id,
                    text=ONBOARDING_WELCOME_TEXT,
                    gateway_timeout_ms=settings.openclaw_gateway_call_timeout_ms,
                    account_id=identity.channel_account_id,
                    session_key=openclaw_session_key,
                    channel=identity.channel,
                )
            else:
                enqueue_onboarding_welcome(
                    account_id=account_id,
                    channel=identity.channel,
                    channel_account_id=identity.channel_account_id,
                    to_user_id=welcome_to_user_id,
                    session_key=openclaw_session_key,
                    text=ONBOARDING_WELCOME_TEXT,
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

    text = (payload.text or "").strip()
    # 图片轮：调 VL 产出多维描述，合成进 user 历史（支撑图后追问 C 场景），
    # 再走主链路按人设接话；VL 失败/总开关关闭则走兜底，跳过主模型（红线：不瞎猜）。
    image_described = False
    image_understanding_failed = False
    if payload.message_type == "image":
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
    elif not text and payload.message_type == "voice":
        text = "[voice message]"

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
    if inserted_id is None:
        duplicate_reply = get_duplicate_reply(
            account_id=account_id,
            reply_to_message_id=message_id,
        )
        latency_ms = int((time.monotonic() - started_at) * 1000)
        return OpenClawTurnResponse(
            status="duplicate",
            reply=duplicate_reply or "刚刚这条消息我已经收到啦。",
            metadata={**identity_response_metadata(identity, account_id), "latency_ms": latency_ms},
        )

    # 入站内容同步筛查（阿里云云审核为主 + 本地红线补充）。命中即停止本轮回复并进入人工队列。
    # 阿里云未开启时内部回退第一阶段异步审核并放行；筛查自身异常时 fail-open（放行本轮回复）。
    inbound_screen = None
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

    # VL 成功后记一次独立的图片理解成本事件（固定贝壳，带总开关，与 chat 扣费相互独立）。
    if image_described:
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

    generation_error = None
    normal_reply_generated = False
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
            profile = session_state.get("profile") or {}
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
            )
            history = llm_input["history"]
            system_prompt = llm_input["system_prompt"]
            llm_messages = llm_input["messages"]
            active_content_invitation = llm_input["active_content_invitation"]
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

            if onboarding_active:
                reply = generate_reply(
                    user_text=text,
                    history=history,
                    system_prompt=system_prompt,
                )
                if onboarding_state == ONBOARDING_PENDING:
                    reply = _ensure_pending_onboarding_question(reply)
            else:
                tools = get_default_tools(
                    web_search_enabled=web_search_enabled_for_turn,
                    content_invitation_response_enabled=bool(active_content_invitation),
                )
                reply, generation_error = generate_reply_with_tools(
                    user_text=text,
                    history=history,
                    system_prompt=system_prompt,
                    tools=tools,
                    ctx=ctx,
                )
            normal_reply_generated = True
        except Exception as err:
            logger.exception("reply generation failed: %s", err)
            generation_error = str(err)
            reply = "我这边刚刚有点卡住了，你可以稍后再发我一次。"

    latency_ms = int((time.monotonic() - started_at) * 1000)
    logger.info(
        "openclaw_turn completed account=%s session=%s status=ok latency_ms=%s error=%s",
        account_id,
        openclaw_session_key,
        latency_ms,
        generation_error,
    )

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

    trace_id = None
    outbound_inserted_id = None
    with db_connect() as conn:
        if debug_trace_enabled:
            trace_id = f"trace-{uuid.uuid4()}"
            insert_debug_trace(
                trace_id=trace_id,
                account_id=account_id,
                session_id=session["id"],
                message_id=message_id,
                source="ai4all",
                llm_model=settings.llm_model,
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

    billing_result = None
    if not generation_error and normal_reply_generated and text and text not in _SPECIAL_COMMANDS:
        try:
            billing_result = record_chat_usage_charge(
                account_id=account_id,
                model=settings.llm_model,
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

    # Advance onboarding state synchronously after reply so onboarding completion
    # does not depend on the after-turn background loop.
    if not generation_error and normal_reply_generated and onboarding_active:
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

    if (
        not generation_error
        and not inbound_blocked
        and text
        and text not in _SPECIAL_COMMANDS
        and background_loop is not None
    ):
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

    return OpenClawTurnResponse(
        status="ok",
        reply=reply,
        metadata={
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
        },
    )
