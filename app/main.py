import logging
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status

from app.config import settings
from app.db import (
    clear_session_messages,
    get_duplicate_reply,
    get_or_create_session,
    init_db,
    insert_message,
    list_recent_messages,
)
from app.llm import generate_reply
from app.schemas import OpenClawTurnRequest, OpenClawTurnResponse


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ai4all")

app = FastAPI(title="AI4ALL Weixin Bot", version="0.1.0")


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


@app.post("/openclaw/turn", response_model=OpenClawTurnResponse)
def openclaw_turn(
    payload: OpenClawTurnRequest,
    _: None = Depends(verify_bridge_auth),
) -> OpenClawTurnResponse:
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
        return OpenClawTurnResponse(
            status="duplicate",
            reply=duplicate_reply,
            metadata={"session_key": session_key},
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
        return OpenClawTurnResponse(
            status="duplicate",
            reply=duplicate_reply or "刚刚这条消息我已经收到啦。",
            metadata={"session_key": session_key},
        )

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
            reply = generate_reply(user_text=text, history=history)
        except Exception as err:
            logger.exception("reply generation failed: %s", err)
            reply = "我这边刚刚有点卡住了，你可以稍后再发我一次。"

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
    )

    return OpenClawTurnResponse(
        status="ok",
        reply=reply,
        metadata={
            "account_id": account_id,
            "session_key": session_key,
            "message_type": payload.message_type,
        },
    )
