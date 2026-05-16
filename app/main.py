import logging
import time
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.config import settings
from app.db import (
    clear_session_messages,
    get_profile_for_session,
    get_session,
    get_duplicate_reply,
    get_or_create_session,
    init_db,
    insert_message,
    list_session_messages,
    list_sessions,
    list_recent_messages,
    update_profile_for_session,
)
from app.llm import generate_reply
from app.schemas import OpenClawTurnRequest, OpenClawTurnResponse


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ai4all")

app = FastAPI(title="AI4ALL Weixin Bot", version="0.1.0")


class ProfileUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    style: Optional[str] = None
    system_prompt: Optional[str] = None
    preferences: Optional[dict] = Field(default=None)


@app.on_event("startup")
def startup() -> None:
    init_db()


def verify_bridge_auth(authorization: Optional[str] = Header(default=None)) -> None:
    expected = f"Bearer {settings.ai4all_bridge_secret}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bridge authorization",
        )


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "env": settings.app_env}


@app.get("/debug/sessions")
def debug_sessions(limit: int = 50) -> dict:
    return {"sessions": list_sessions(limit=limit)}


@app.get("/debug/messages")
def debug_messages(session_id: int, limit: int = 100) -> dict:
    session = get_session(session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {
        "session": session,
        "profile": get_profile_for_session(session_id=session_id),
        "messages": list_session_messages(session_id=session_id, limit=limit),
    }


@app.post("/debug/sessions/{session_id}/reset")
def debug_reset_session(session_id: int) -> dict:
    if get_session(session_id=session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    deleted = clear_session_messages(session_id=session_id)
    return {"status": "ok", "session_id": session_id, "deleted": deleted}


@app.get("/debug/sessions/{session_id}/profile")
def debug_get_profile(session_id: int) -> dict:
    profile = get_profile_for_session(session_id=session_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return {"profile": profile}


@app.post("/debug/sessions/{session_id}/profile")
def debug_update_profile(session_id: int, payload: ProfileUpdateRequest) -> dict:
    profile = update_profile_for_session(
        session_id=session_id,
        display_name=payload.display_name,
        style=payload.style,
        system_prompt=payload.system_prompt,
        preferences=payload.preferences,
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return {"status": "ok", "profile": profile}


@app.post("/openclaw/turn", response_model=OpenClawTurnResponse)
def openclaw_turn(
    payload: OpenClawTurnRequest,
    _: None = Depends(verify_bridge_auth),
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

    account_id = payload.account_id or payload.channel or "default"
    session_key = payload.session_key or payload.chat_id or payload.sender_id or "unknown"
    sender_id = payload.sender_id or payload.chat_id or session_key
    message_id = payload.message_id or payload.event_id

    session_state = get_or_create_session(
        account_id=account_id,
        channel=payload.channel or "unknown",
        sender_id=sender_id,
        sender_name=payload.sender_name,
        chat_id=payload.chat_id,
        session_key=session_key,
    )
    session = session_state["session"]

    duplicate_reply = get_duplicate_reply(
        account_id=account_id,
        reply_to_message_id=message_id,
    )
    if duplicate_reply:
        latency_ms = int((time.monotonic() - started_at) * 1000)
        return OpenClawTurnResponse(
            status="duplicate",
            reply=duplicate_reply,
            metadata={"session_key": session_key, "latency_ms": latency_ms},
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
        raw=payload.raw,
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
            metadata={"session_key": session_key, "latency_ms": latency_ms},
        )

    generation_error = None
    if text == "#重置会话":
        clear_session_messages(session_id=session["id"])
        reply = "已重置当前会话。"
    elif text == "#状态":
        reply = f"当前会话正常。account_id={account_id}, session_key={session_key}"
    else:
        try:
            history = list_recent_messages(
                session_id=session["id"],
                limit=settings.llm_context_messages,
            )
            profile = session_state.get("profile") or {}
            reply = generate_reply(
                user_text=text,
                history=history,
                system_prompt=profile.get("system_prompt"),
                style=profile.get("style"),
            )
        except Exception as err:
            logger.exception("reply generation failed: %s", err)
            generation_error = str(err)
            reply = "我这边刚刚有点卡住了，你可以稍后再发我一次。"

    latency_ms = int((time.monotonic() - started_at) * 1000)
    logger.info(
        "openclaw_turn completed account=%s session=%s status=ok latency_ms=%s error=%s",
        account_id,
        session_key,
        latency_ms,
        generation_error,
    )

    reply_message_id = f"reply-{uuid.uuid4()}"
    insert_message(
        account_id=account_id,
        session_id=session["id"],
        message_id=reply_message_id,
        reply_to_message_id=message_id,
        direction="outbound",
        role="assistant",
        message_type="text",
        content=reply,
        raw={"source": "ai4all"},
        latency_ms=latency_ms,
        error=generation_error,
    )

    return OpenClawTurnResponse(
        status="ok",
        reply=reply,
        metadata={
            "account_id": account_id,
            "session_key": session_key,
            "message_type": payload.message_type,
            "latency_ms": latency_ms,
        },
    )
