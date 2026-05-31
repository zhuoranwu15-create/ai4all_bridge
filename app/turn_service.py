import asyncio
import logging
import time
import uuid
from datetime import datetime
from typing import Optional

from app.config import settings
from app.db import (
    ACCOUNT_ACTIVE_SESSION_KEY,
    clear_session_messages,
    get_account_onboarding_state,
    get_daily_usage,
    get_duplicate_reply,
    increment_session_turn_count,
    increment_daily_usage,
    insert_debug_trace,
    insert_message,
    list_recent_messages,
    record_chat_usage_charge,
    resolve_account_id_for_inbound_channel_identity,
    set_account_onboarding_state,
    upsert_channel_binding,
)
from app.identity import identity_response_metadata, resolve_openclaw_identity
from app.llm import generate_reply, generate_reply_with_tools
from app.memory_writer import write_memory
from app.prompt_builder import PromptBuilder, extract_section
from app.proactive.commitments import extract_commitment_from_turn
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
from app.user_profiles import (
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


def _tool_instructions(*, web_search_enabled: bool) -> str:
    instructions = [
        "## 提醒工具使用规则",
        "",
        "- 用户明确要求在未来某个时间收到提醒时，调用 create_reminder。",
        "- 时间不明确时，不要猜测，告知用户需要补充具体日期和时间。",
        "- 取消或修改提醒前，先调用 list_reminders 确认提醒存在再操作。",
        "- 多个提醒且用户描述不精确时，列出让用户选择，不要盲目操作。",
    ]
    if web_search_enabled:
        instructions.extend(
            [
                "",
                "## 网络搜索工具使用规则",
                "",
                "- 用户询问最新、实时、外部世界事实，或明确要求搜索/查找时，调用 web_search。",
                "- 搜索后基于结果回答，并在回答里保留关键来源链接。",
                "- 搜索失败时，说明未能完成实时搜索，不要编造搜索结果。",
            ]
        )
    else:
        instructions.append("- 不要承诺任何工具之外的功能（如网络搜索、发图片等）。")
    return "\n".join(instructions)


async def _advance_onboarding_state_async(
    *,
    account_id: str,
    user_text: str,
    current_state: str,
) -> None:
    """Background task: extract onboarding info from user reply and advance state."""
    try:
        extracted = await extract_onboarding_info_async(
            user_text=user_text,
            current_state=current_state,
        )
        apply_extracted_onboarding_info(
            account_id=account_id,
            extracted=extracted,
            current_state=current_state,
        )
        new_state = next_onboarding_state(
            current_state=current_state,
            extracted=extracted,
            user_name_ask_count=0,
            persona_ask_count=0,
        )
        if new_state != current_state:
            set_account_onboarding_state(account_id=account_id, state=new_state)
            logger.info(
                "onboarding state advanced account=%s %s -> %s",
                account_id,
                current_state,
                new_state,
            )
    except Exception as err:
        logger.exception("onboarding state advance failed account=%s error=%s", account_id, err)


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


def handle_openclaw_turn(
    payload: OpenClawTurnRequest,
    *,
    background_loop: Optional[asyncio.AbstractEventLoop] = None,
    force_web_search_enabled: Optional[bool] = None,
) -> OpenClawTurnResponse:
    started_at = time.monotonic()
    logger.info(
        "openclaw_turn received channel=%s session=%s sender=%s type=%s text=%r raw_keys=%s",
        payload.channel,
        payload.session_key,
        payload.sender_id,
        payload.message_type,
        payload.text,
        sorted(payload.raw.keys()),
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
    account_id = resolve_account_id_for_inbound_channel_identity(
        channel=identity.channel,
        session_key=identity.session_key,
        channel_account_id=identity.channel_account_id,
    )
    sender_id = identity.sender_id
    message_id = payload.message_id or payload.event_id

    now = datetime.now()
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
            send_weixin_text(
                to_user_id=welcome_to_user_id,
                text=ONBOARDING_WELCOME_TEXT,
                gateway_timeout_ms=settings.openclaw_gateway_call_timeout_ms,
                account_id=identity.channel_account_id,
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

    effective_rpm = settings.rate_limit_rpm if account.get("rpm_limit") is None else account["rpm_limit"]
    effective_daily = settings.rate_limit_daily if account.get("daily_limit") is None else account["daily_limit"]

    if effective_rpm > 0 and not rate_limiter.check_rpm(account_id, effective_rpm):
        logger.info("openclaw_turn rpm_limited account=%s", account_id)
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
    if not text and payload.message_type == "voice":
        text = "[voice message]"

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
    )
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

    increment_daily_usage(account_id=account_id, date=today)

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
    if text == "#重置会话":
        clear_session_messages(session_id=session["id"])
        reply = "已重置当前会话。"
    elif text == "#状态":
        reply = (
            f"当前会话正常。account_id={account_id}, "
            f"active_session_key={ACCOUNT_ACTIVE_SESSION_KEY}, "
            f"openclaw_session_key={openclaw_session_key}"
        )
    else:
        try:
            history = list_recent_messages(
                session_id=session["id"],
                limit=settings.llm_context_messages,
            )
            profile = session_state.get("profile") or {}
            file_profile = read_user_profile(account_id)
            soul = extract_section(file_profile, "Soul")
            user_prefs = extract_section(file_profile, "User Preferences")
            long_term_memory = extract_section(file_profile, "Long-term Memory")
            agent_context = read_agent_context(
                account_id,
                display_name=account.get("display_name"),
            )
            onboarding_pre_written = {}
            if onboarding_active and onboarding_state in {ONBOARDING_STEP1_SENT, ONBOARDING_STEP2_SENT}:
                onboarding_pre_extracted = _extract_onboarding_info_sync(
                    user_text=text,
                    current_state=onboarding_state,
                )
                # For step1: pre-write user_name; for step2: pre-write ai_name.
                # Writing before prompt build ensures the AI sees what was just collected.
                _pre_write_key = "user_name" if onboarding_state == ONBOARDING_STEP1_SENT else "ai_name"
                if onboarding_pre_extracted.get(_pre_write_key):
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
            debug_metadata.update(
                {
                    "history_count": len(history),
                    "soul_chars": len(soul),
                    "user_prefs_chars": len(user_prefs),
                    "long_term_memory_chars": len(long_term_memory),
                    "daily_notes_loaded": False,
                    "daily_notes_chars": 0,
                    "agent_context": agent_context.metadata(),
                    "system_prompt_override": bool(profile.get("system_prompt")),
                    "style": profile.get("style"),
                    "display_name": account.get("display_name"),
                    "onboarding_pre_written": onboarding_pre_written,
                }
            )
            onboarding_ctx = ""
            if onboarding_active:
                onboarding_ctx = build_onboarding_prompt_context(
                    state=onboarding_state,
                    user_name=_extract_user_name_from_context(agent_context.blocks),
                    ai_name=_extract_ai_name_from_context(agent_context.blocks),
                    persona=None,
                    user_name_ask_count=_count_user_name_asks(session.get("turn_count", 0), onboarding_state),
                    persona_ask_count=0 if onboarding_state != ONBOARDING_STEP3_SENT else 1,
                )
            builder = PromptBuilder()
            system_prompt = builder.build(
                display_name=account.get("display_name"),
                soul=soul,
                user_prefs=user_prefs,
                long_term_memory=long_term_memory,
                daily_notes=None,
                carryover_summary=session.get("carryover_summary"),
                system_prompt_override=profile.get("system_prompt"),
                style=profile.get("style"),
                agent_context=agent_context.blocks,
                onboarding_context=onboarding_ctx,
                today=today,
                model_name=settings.llm_model,
                tool_instructions=(
                    None if onboarding_active else _tool_instructions(
                        web_search_enabled=web_search_enabled_for_turn
                    )
                ),
            )
            llm_messages = [{"role": "system", "content": system_prompt}]
            llm_messages.extend(history)

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
                # Step2 post-response fallback: if ai_name was not pre-written (extraction
                # failed or returned null), apply rule-based extraction now so IDENTITY.md
                # gets the correct name even though the persona options already showed presets.
                if onboarding_state == ONBOARDING_STEP2_SENT and not onboarding_pre_written.get("ai_name"):
                    from app.onboarding import _rule_extract_name  # noqa: PLC0415
                    fallback_ai_name = _rule_extract_name(text)
                    if fallback_ai_name:
                        try:
                            from app.user_profiles import write_ai_name_to_identity  # noqa: PLC0415
                            write_ai_name_to_identity(account_id=account_id, name=fallback_ai_name)
                            logger.info(
                                "onboarding ai_name fallback-written account=%s name=%r",
                                account_id, fallback_ai_name,
                            )
                        except Exception as _fe:
                            logger.error("onboarding ai_name fallback write failed account=%s error=%s", account_id, _fe)
            else:
                tools = get_default_tools(web_search_enabled=web_search_enabled_for_turn)
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
    trace_id = None
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
        )
        logger.info(
            "debug trace recorded trace_id=%s account=%s session=%s message_id=%s messages=%s prompt_chars=%s",
            trace_id,
            account_id,
            openclaw_session_key,
            message_id,
            len(llm_messages),
            len(system_prompt or ""),
        )

    insert_message(
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
            extra={
                "message_id": reply_message_id,
                "reply_to_message_id": message_id,
            },
        ),
        latency_ms=latency_ms,
        error=generation_error,
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

    if not generation_error and text and text not in _SPECIAL_COMMANDS:
        increment_session_turn_count(session_id=int(session["id"]))

    # Advance onboarding state synchronously after reply (pending->step1_sent needs no extraction)
    if not generation_error and normal_reply_generated and onboarding_active:
        if onboarding_state == "pending":
            try:
                set_account_onboarding_state(account_id=account_id, state=ONBOARDING_STEP1_SENT)
                logger.info("onboarding state advanced account=%s pending -> step1_sent", account_id)
            except Exception as err:
                logger.error("onboarding state set failed account=%s error=%s", account_id, err)

    if not generation_error and text and text not in _SPECIAL_COMMANDS and background_loop is not None:
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

        if onboarding_active and normal_reply_generated and onboarding_state != "pending":
            if onboarding_pre_extracted is not None:
                try:
                    new_state = next_onboarding_state(
                        current_state=onboarding_state,
                        extracted=onboarding_pre_extracted,
                        user_name_ask_count=0,
                        persona_ask_count=0,
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
            else:
                # steps 1 and 3: extract info and advance state in background
                background_loop.call_soon_threadsafe(
                    background_loop.create_task,
                    _advance_onboarding_state_async(
                        account_id=account_id,
                        user_text=text,
                        current_state=onboarding_state,
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
