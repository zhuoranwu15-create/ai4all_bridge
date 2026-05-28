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
    create_reminder,
    get_daily_usage,
    get_duplicate_reply,
    increment_session_turn_count,
    increment_daily_usage,
    insert_debug_trace,
    insert_message,
    list_recent_messages,
    resolve_account_id_for_inbound_channel_identity,
    upsert_channel_binding,
)
from app.identity import identity_response_metadata, resolve_openclaw_identity
from app.llm import generate_reply
from app.memory_writer import write_memory
from app.prompt_builder import PromptBuilder, extract_section
from app.proactive.commitments import extract_commitment_from_turn
from app.rate_limiter import rate_limiter
from app.reminder_parser import looks_like_reminder_request, parse_explicit_reminder
from app.schemas import OpenClawTurnRequest, OpenClawTurnResponse
from app.session_lifecycle import business_day_for, get_or_create_account_active_session_with_dreaming
from app.user_profiles import (
    ensure_user_profile,
    read_agent_context,
    read_user_profile,
)


logger = logging.getLogger("ai4all.turn_service")

_SPECIAL_COMMANDS = {"#重置会话", "#状态"}


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
    system_prompt = None
    llm_messages = []
    debug_metadata = {
        "trace_kind": "ai4all_turn",
        "debug_trace_enabled": debug_trace_enabled,
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
        parsed_reminder = (
            parse_explicit_reminder(text)
            if payload.message_type == "text"
            else None
        )
        reminder_intent = (
            looks_like_reminder_request(text)
            if payload.message_type == "text"
            else False
        )
        if parsed_reminder is not None:
            to_user_id = binding.get("chat_id") or identity.chat_id
            if not to_user_id:
                generation_error = "reminder_route_missing"
                reply = "我理解你想设置提醒，但现在缺少可主动发送的微信会话目标。你可以再发我一条消息后重试。"
            elif not identity.channel_account_id:
                generation_error = "reminder_channel_account_missing"
                reply = "我理解你想设置提醒，但现在缺少微信通道账号信息，暂时还没法保证到点发出。"
            else:
                try:
                    reminder = create_reminder(
                        account_id=account_id,
                        channel=identity.channel,
                        channel_account_id=identity.channel_account_id,
                        to_user_id=to_user_id,
                        session_key=openclaw_session_key,
                        text=parsed_reminder.text,
                        due_at=parsed_reminder.due_at_db,
                        metadata={
                            "source": "openclaw_turn",
                            "source_message_id": message_id,
                            "parser": "explicit_rule_v1",
                            "matched_time_text": parsed_reminder.matched_time_text,
                            "raw_text": text,
                        },
                    )
                    debug_metadata.update(
                        {
                            "reminder_id": reminder["id"],
                            "reminder_due_at": reminder["due_at"],
                            "reminder_text": reminder["text"],
                            "reminder_parser": "explicit_rule_v1",
                        }
                    )
                    reply = (
                        f"好的，我会在 {parsed_reminder.due_at_display} "
                        f"提醒你：{parsed_reminder.text}"
                    )
                except Exception as err:
                    logger.exception("create reminder failed: %s", err)
                    generation_error = str(err)
                    reply = "我理解你想设置提醒，但这次保存失败了。你可以稍后再试一次。"
        elif reminder_intent:
            reply = "可以，我现在支持明确时间的一次性提醒。请补充具体日期和时间，比如：今天下午3点提醒我去趟派出所。"
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
                    }
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
                    today=today,
                    model_name=settings.llm_model,
                )
                llm_messages = [{"role": "system", "content": system_prompt}]
                llm_messages.extend(history)
                reply = generate_reply(
                    user_text=text,
                    history=history,
                    system_prompt=system_prompt,
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

    if not generation_error and text and text not in _SPECIAL_COMMANDS:
        increment_session_turn_count(session_id=int(session["id"]))

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
        },
    )
