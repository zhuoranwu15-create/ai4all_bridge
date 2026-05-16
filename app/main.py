import logging
import time
import uuid
from datetime import date as date_cls
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import settings
from app.db import (
    clear_session_messages,
    get_account,
    get_daily_usage,
    get_message_raw,
    get_profile_for_account,
    get_profile_for_session,
    get_session,
    get_duplicate_reply,
    get_or_create_session,
    get_usage_last_7_days,
    increment_daily_usage,
    init_db,
    insert_message,
    list_accounts,
    list_recent_message_raw,
    list_session_messages,
    list_sessions,
    list_sessions_for_account,
    list_recent_messages,
    set_account_status,
    update_account,
    update_profile_for_account,
    update_profile_for_session,
)
from app.llm import generate_reply
from app.rate_limiter import rate_limiter
from app.schemas import OpenClawTurnRequest, OpenClawTurnResponse
from app.user_profiles import ensure_user_profile, read_user_profile


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ai4all")

app = FastAPI(title="AI4ALL Weixin Bot", version="0.1.0")


class ProfileUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    style: Optional[str] = None
    system_prompt: Optional[str] = None
    preferences: Optional[dict] = Field(default=None)


class AccountUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    notes: Optional[str] = None
    daily_limit: Optional[int] = None
    rpm_limit: Optional[int] = None


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


def verify_admin_auth(authorization: Optional[str] = Header(default=None)) -> None:
    expected = f"Bearer {settings.admin_token}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin authorization",
        )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "env": settings.app_env}


# ---------------------------------------------------------------------------
# Debug (no auth)
# ---------------------------------------------------------------------------

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


@app.get("/debug/messages/raw")
def debug_recent_message_raw(limit: int = 20) -> dict:
    return {"messages": list_recent_message_raw(limit=limit)}


@app.get("/debug/messages/{message_db_id}/raw")
def debug_message_raw(message_db_id: int) -> dict:
    message = get_message_raw(message_db_id=message_db_id)
    if message is None:
        raise HTTPException(status_code=404, detail="message not found")
    return {"message": message}


@app.get("/debug/accounts/{account_id}/user-profile")
def debug_get_user_profile(account_id: str) -> dict:
    path = ensure_user_profile(account_id)
    return {
        "account_id": account_id,
        "path": str(path),
        "content": path.read_text(encoding="utf-8"),
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


# ---------------------------------------------------------------------------
# Admin — accounts
# ---------------------------------------------------------------------------

@app.get("/admin/accounts")
def admin_accounts(_: None = Depends(verify_admin_auth)) -> dict:
    return {"accounts": list_accounts()}


@app.get("/admin/accounts/{account_id}")
def admin_account(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    account = get_account(account_id=account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {
        "account": account,
        "profile": get_profile_for_account(account_id=account_id),
        "sessions": list_sessions_for_account(account_id=account_id),
    }


@app.patch("/admin/accounts/{account_id}")
def admin_update_account(
    account_id: str,
    payload: AccountUpdateRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    updates = payload.model_dump(exclude_unset=True)
    account = update_account(account_id=account_id, **updates)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {"status": "ok", "account": account}


@app.post("/admin/accounts/{account_id}/disable")
def admin_disable_account(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    account = set_account_status(account_id=account_id, status="disabled")
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {"status": "ok", "account": account}


@app.post("/admin/accounts/{account_id}/enable")
def admin_enable_account(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    account = set_account_status(account_id=account_id, status="active")
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {"status": "ok", "account": account}


@app.patch("/admin/accounts/{account_id}/profile")
def admin_update_account_profile(
    account_id: str,
    payload: ProfileUpdateRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    profile = update_profile_for_account(
        account_id=account_id,
        display_name=payload.display_name,
        style=payload.style,
        system_prompt=payload.system_prompt,
        preferences=payload.preferences,
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="account/profile not found")
    return {"status": "ok", "profile": profile}


@app.get("/admin/accounts/{account_id}/sessions")
def admin_account_sessions(
    account_id: str,
    limit: int = 50,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {"sessions": list_sessions_for_account(account_id=account_id, limit=limit)}


@app.get("/admin/accounts/{account_id}/user-profile")
def admin_get_user_profile(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    path = ensure_user_profile(account_id)
    return {
        "account_id": account_id,
        "path": str(path),
        "content": path.read_text(encoding="utf-8"),
    }


@app.get("/admin/accounts/{account_id}/usage")
def admin_account_usage(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    today = date_cls.today().isoformat()
    return {
        "account_id": account_id,
        "today": {
            "date": today,
            "message_count": get_daily_usage(account_id=account_id, date=today),
        },
        "last_7_days": get_usage_last_7_days(account_id=account_id),
    }


# ---------------------------------------------------------------------------
# Admin — sessions
# ---------------------------------------------------------------------------

@app.get("/admin/sessions")
def admin_sessions(limit: int = 100, _: None = Depends(verify_admin_auth)) -> dict:
    return {"sessions": list_sessions(limit=limit)}


@app.get("/admin/sessions/{session_id}")
def admin_session(session_id: int, _: None = Depends(verify_admin_auth)) -> dict:
    session = get_session(session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {
        "session": session,
        "profile": get_profile_for_session(session_id=session_id),
        "messages": list_session_messages(session_id=session_id, limit=100),
    }


@app.post("/admin/sessions/{session_id}/reset")
def admin_reset_session(session_id: int, _: None = Depends(verify_admin_auth)) -> dict:
    if get_session(session_id=session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    deleted = clear_session_messages(session_id=session_id)
    return {"status": "ok", "session_id": session_id, "deleted": deleted}


# ---------------------------------------------------------------------------
# Admin — messages
# ---------------------------------------------------------------------------

@app.get("/admin/messages/raw")
def admin_recent_message_raw(
    limit: int = 20,
    _: None = Depends(verify_admin_auth),
) -> dict:
    return {"messages": list_recent_message_raw(limit=limit)}


@app.get("/admin/messages/{message_db_id}/raw")
def admin_message_raw(
    message_db_id: int,
    _: None = Depends(verify_admin_auth),
) -> dict:
    message = get_message_raw(message_db_id=message_db_id)
    if message is None:
        raise HTTPException(status_code=404, detail="message not found")
    return {"message": message}


# ---------------------------------------------------------------------------
# Bridge endpoint
# ---------------------------------------------------------------------------

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
    account = session_state["account"]
    session = session_state["session"]
    profile_path = ensure_user_profile(account_id)

    if account.get("status") == "disabled":
        logger.info(
            "openclaw_turn ignored disabled account account=%s session=%s",
            account_id,
            session_key,
        )
        return OpenClawTurnResponse(
            status="disabled",
            no_reply=True,
            metadata={"account_id": account_id, "session_key": session_key},
        )

    today = date_cls.today().isoformat()
    effective_rpm = settings.rate_limit_rpm if account.get("rpm_limit") is None else account["rpm_limit"]
    effective_daily = settings.rate_limit_daily if account.get("daily_limit") is None else account["daily_limit"]

    if effective_rpm > 0 and not rate_limiter.check_rpm(account_id, effective_rpm):
        logger.info("openclaw_turn rpm_limited account=%s", account_id)
        return OpenClawTurnResponse(
            status="rate_limited",
            reply=settings.rate_limit_rpm_message,
            metadata={"account_id": account_id, "reason": "rpm"},
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
                metadata={"account_id": account_id, "reason": "daily", "count": current_count},
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

    increment_daily_usage(account_id=account_id, date=today)

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
            file_profile = read_user_profile(account_id)
            reply = generate_reply(
                user_text=text,
                history=history,
                system_prompt=profile.get("system_prompt"),
                style=profile.get("style"),
                user_profile=file_profile,
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
            "user_profile_path": str(profile_path),
        },
    )
