import asyncio
import logging
import time
import uuid
from datetime import date as date_cls
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import settings
from app.captcha import verify_captcha
from app.sms import generate_otp, send_otp
from app.db import (
    clear_session_messages,
    consume_verification_token,
    count_verifications_last_hour,
    create_ai4all_account_for_user,
    create_binding_intent,
    create_or_get_platform_user_by_phone,
    create_phone_verification,
    get_debug_trace,
    get_account,
    get_binding_intent,
    get_daily_usage,
    get_latest_active_verification,
    get_latest_subscription_for_user,
    get_message_raw,
    get_platform_user,
    get_profile_for_account,
    get_profile_for_session,
    get_session,
    get_duplicate_reply,
    get_or_create_session,
    get_usage_last_7_days,
    get_valid_verification_by_token,
    increment_daily_usage,
    increment_verify_attempts,
    init_db,
    insert_debug_trace,
    insert_message,
    invalidate_verifications_for_phone,
    list_accounts,
    list_debug_traces,
    list_recent_message_raw,
    list_session_messages,
    list_sessions,
    list_sessions_for_account,
    list_recent_messages,
    normalize_phone,
    resolve_account_id_for_inbound_channel_identity,
    set_binding_intent_error,
    set_account_status,
    set_verification_verified,
    update_binding_intent,
    update_account,
    update_profile_for_account,
    update_profile_for_session,
    list_channel_bindings_for_account,
    upsert_channel_binding,
)
from app.identity import resolve_openclaw_identity
from app.llm import generate_reply
from app.openclaw_gateway import (
    start_weixin_qr_login,
    wait_weixin_qr_login,
)
from app.prompt_builder import PromptBuilder, extract_section
from app.rate_limiter import rate_limiter
from app.schemas import OpenClawDebugTraceRequest, OpenClawTurnRequest, OpenClawTurnResponse
from app.memory_writer import write_memory
from app.dreaming import run_dreaming
from app.user_profiles import ensure_user_profile, read_user_profile, read_daily_notes, read_agent_context


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ai4all")

_background_loop: Optional[asyncio.AbstractEventLoop] = None

_SPECIAL_COMMANDS = {"#重置会话", "#状态"}

app = FastAPI(title="AI4ALL Weixin Bot", version="0.1.0")
app.mount("/ui", StaticFiles(directory="app/static", html=True), name="ui")


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


class WebRegisterRequest(BaseModel):
    phone: str
    display_name: Optional[str] = None
    otp_token: str


class SendOtpRequest(BaseModel):
    phone: str
    captcha_verify_param: str


class VerifyOtpRequest(BaseModel):
    phone: str
    code: str


class WebCreateAgentRequest(BaseModel):
    platform_user_id: str
    agent_name: str
    role_prompt: Optional[str] = None
    plan: Optional[str] = "free"


class WebCreateBindingIntentRequest(BaseModel):
    platform_user_id: str
    account_id: str
    channel: Optional[str] = "openclaw-weixin"


def _debug_trace_account_ids() -> set[str]:
    raw = getattr(settings, "debug_trace_account_ids", "") or ""
    return {item.strip() for item in raw.split(",") if item.strip()}


def _is_debug_trace_account(account_id: str) -> bool:
    return account_id in _debug_trace_account_ids()


def _identity_response_metadata(identity, account_id: Optional[str] = None) -> dict:
    metadata = identity.metadata()
    if account_id and account_id != identity.account_id:
        metadata["ai4all_account_id"] = account_id
        metadata["account_id"] = account_id
        metadata["openclaw_session_key_account_id"] = identity.account_id
    return metadata


def _complete_binding_intent_from_wait_result(binding_intent: dict, result: dict) -> None:
    raw_channel_account_id = result.get("accountId") or result.get("channel_account_id")
    channel_account_id = str(raw_channel_account_id).strip() if raw_channel_account_id else None
    raw_result = {
        **result,
        "channel_account_id": channel_account_id,
        "binding_intent_id": binding_intent["id"],
    }
    if result.get("connected") and channel_account_id:
        update_binding_intent(
            binding_intent_id=binding_intent["id"],
            status="completed",
            channel_account_id=channel_account_id,
            raw_result=raw_result,
            completed=True,
            error=None,
        )
        upsert_channel_binding(
            account_id=binding_intent["account_id"],
            channel=binding_intent["channel"],
            session_key=binding_intent["openclaw_login_session_key"],
            channel_account_id=channel_account_id,
            sender_id=None,
            chat_id=None,
            raw_identity={
                "binding_intent_id": binding_intent["id"],
                "platform_user_id": binding_intent["platform_user_id"],
                "ai4all_account_id": binding_intent["account_id"],
                "openclaw_login_session_key": binding_intent["openclaw_login_session_key"],
                "channel_account_id": channel_account_id,
            },
        )
        return

    status = "already_connected" if result.get("alreadyConnected") else "failed"
    set_binding_intent_error(
        binding_intent_id=binding_intent["id"],
        status=status,
        error=str(result.get("message") or status),
        raw_result=raw_result,
    )


async def _wait_for_binding_intent(binding_intent_id: str) -> None:
    binding_intent = get_binding_intent(binding_intent_id=binding_intent_id)
    if binding_intent is None:
        return
    try:
        result = await asyncio.to_thread(
            wait_weixin_qr_login,
            account_id=binding_intent["openclaw_login_session_key"],
            current_qr_data_url=binding_intent.get("qr_data_url"),
            gateway_timeout_ms=settings.openclaw_gateway_call_timeout_ms,
            wait_timeout_ms=settings.openclaw_login_wait_timeout_ms,
        )
    except Exception as err:
        logger.exception("OpenClaw QR wait failed intent=%s", binding_intent_id)
        set_binding_intent_error(
            binding_intent_id=binding_intent_id,
            status="failed",
            error=str(err),
        )
        return
    latest = get_binding_intent(binding_intent_id=binding_intent_id)
    if latest is None:
        return
    _complete_binding_intent_from_wait_result(latest, result)


def _schedule_binding_wait(binding_intent_id: str) -> None:
    if _background_loop is None:
        return
    _background_loop.call_soon_threadsafe(
        _background_loop.create_task,
        _wait_for_binding_intent(binding_intent_id),
    )


def _start_openclaw_qr_for_binding(binding_intent: dict) -> dict:
    if not settings.openclaw_login_auto_start:
        return binding_intent
    try:
        start_result = start_weixin_qr_login(
            account_id=binding_intent["openclaw_login_session_key"],
            gateway_timeout_ms=settings.openclaw_gateway_call_timeout_ms,
            start_timeout_ms=settings.openclaw_login_start_timeout_ms,
            force=False,
        )
    except Exception as err:
        logger.warning("OpenClaw QR start failed intent=%s error=%s", binding_intent["id"], err)
        failed = set_binding_intent_error(
            binding_intent_id=binding_intent["id"],
            status="failed",
            error=str(err),
        )
        return failed or binding_intent

    session_key = str(start_result.get("sessionKey") or binding_intent["openclaw_login_session_key"])
    updated = update_binding_intent(
        binding_intent_id=binding_intent["id"],
        status="qr_created" if start_result.get("qrDataUrl") else "failed",
        openclaw_login_session_key=session_key,
        qr_data_url=start_result.get("qrDataUrl"),
        raw_result={
            "start": start_result,
            "binding_intent_id": binding_intent["id"],
            "openclaw_login_session_key": session_key,
        },
        error=None if start_result.get("qrDataUrl") else str(start_result.get("message") or "QR not returned"),
    )
    updated = updated or binding_intent
    if updated.get("qr_data_url"):
        _schedule_binding_wait(updated["id"])
    return updated


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.on_event("startup")
async def capture_event_loop() -> None:
    global _background_loop
    _background_loop = asyncio.get_running_loop()


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
# Debug (admin auth required)
# ---------------------------------------------------------------------------

@app.get("/debug/sessions")
def debug_sessions(limit: int = 50, _: None = Depends(verify_admin_auth)) -> dict:
    return {"sessions": list_sessions(limit=limit)}


@app.get("/debug/messages")
def debug_messages(session_id: int, limit: int = 100, _: None = Depends(verify_admin_auth)) -> dict:
    session = get_session(session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {
        "session": session,
        "profile": get_profile_for_session(session_id=session_id),
        "messages": list_session_messages(session_id=session_id, limit=limit),
    }


@app.get("/debug/messages/raw")
def debug_recent_message_raw(limit: int = 20, _: None = Depends(verify_admin_auth)) -> dict:
    return {"messages": list_recent_message_raw(limit=limit)}


@app.get("/debug/messages/{message_db_id}/raw")
def debug_message_raw(message_db_id: int, _: None = Depends(verify_admin_auth)) -> dict:
    message = get_message_raw(message_db_id=message_db_id)
    if message is None:
        raise HTTPException(status_code=404, detail="message not found")
    return {"message": message}


@app.get("/debug/traces")
def debug_traces(
    account_id: Optional[str] = None,
    session_id: Optional[int] = None,
    limit: int = 50,
    _: None = Depends(verify_admin_auth),
) -> dict:
    return {
        "traces": list_debug_traces(
            account_id=account_id,
            session_id=session_id,
            limit=limit,
        )
    }


@app.get("/debug/traces/{trace_id}")
def debug_trace(trace_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    trace = get_debug_trace(trace_id=trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return {"trace": trace}


@app.get("/debug/accounts/{account_id}/prompt-preview")
def debug_prompt_preview(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    """Show the assembled system prompt and per-block sizes for an account."""
    from app.user_profiles import read_user_profile, read_daily_notes, read_agent_context
    account = get_account(account_id=account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    profile = get_profile_for_account(account_id=account_id) or {}
    file_profile = read_user_profile(account_id)
    today = date_cls.today().isoformat()
    soul = extract_section(file_profile, "Soul")
    user_prefs = extract_section(file_profile, "User Preferences")
    long_term_memory = extract_section(file_profile, "Long-term Memory")
    agent_context = read_agent_context(
        account_id,
        display_name=account.get("display_name"),
    )
    daily_notes = read_daily_notes(account_id, today)
    builder = PromptBuilder()
    prompt = builder.build(
        display_name=account.get("display_name"),
        soul=soul,
        user_prefs=user_prefs,
        long_term_memory=long_term_memory,
        daily_notes=daily_notes,
        system_prompt_override=profile.get("system_prompt"),
        style=profile.get("style"),
        agent_context=agent_context.blocks,
        today=today,
        model_name=settings.llm_model,
    )
    return {
        "account_id": account_id,
        "today": today,
        "total_chars": len(prompt),
        "blocks": {
            "soul_chars": len(soul),
            "user_prefs_chars": len(user_prefs),
            "long_term_memory_chars": len(long_term_memory),
            "daily_notes_chars": len(daily_notes),
            "system_prompt_override": bool(profile.get("system_prompt")),
            "style": profile.get("style"),
            "display_name": account.get("display_name"),
            "agent_context": agent_context.metadata(),
        },
        "prompt": prompt,
    }


@app.get("/debug/accounts/{account_id}/user-profile")
def debug_get_user_profile(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    path = ensure_user_profile(account_id)
    context = read_agent_context(account_id)
    return {
        "account_id": account_id,
        "path": str(path),
        "content": path.read_text(encoding="utf-8"),
        "agent_context": context.metadata(),
    }


@app.post("/debug/sessions/{session_id}/reset")
def debug_reset_session(session_id: int, _: None = Depends(verify_admin_auth)) -> dict:
    if get_session(session_id=session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    deleted = clear_session_messages(session_id=session_id)
    return {"status": "ok", "session_id": session_id, "deleted": deleted}


@app.get("/debug/sessions/{session_id}/profile")
def debug_get_profile(session_id: int, _: None = Depends(verify_admin_auth)) -> dict:
    profile = get_profile_for_session(session_id=session_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return {"profile": profile}


@app.post("/debug/sessions/{session_id}/profile")
def debug_update_profile(session_id: int, payload: ProfileUpdateRequest, _: None = Depends(verify_admin_auth)) -> dict:
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
# Web onboarding (MVP)
# ---------------------------------------------------------------------------

@app.post("/web/sms/send-otp")
def web_send_otp(payload: SendOtpRequest) -> dict:
    try:
        phone = normalize_phone(payload.phone)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    if not verify_captcha(payload.captcha_verify_param):
        raise HTTPException(status_code=400, detail="验证码校验未通过")

    count = count_verifications_last_hour(phone)
    if count >= settings.aliyun_sms_max_per_phone_per_hour:
        raise HTTPException(status_code=429, detail="发送频率过高，请稍后重试")

    invalidate_verifications_for_phone(phone)
    code = generate_otp()
    create_phone_verification(phone=phone, code=code, expires_minutes=settings.otp_expires_minutes)
    send_otp(phone=phone, code=code)

    return {"status": "ok"}


@app.post("/web/sms/verify-otp")
def web_verify_otp(payload: VerifyOtpRequest) -> dict:
    try:
        phone = normalize_phone(payload.phone)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    verification = get_latest_active_verification(phone)
    if verification is None:
        raise HTTPException(status_code=400, detail="验证码不存在或已过期，请重新获取")

    if verification["verify_attempts"] >= 5:
        raise HTTPException(status_code=400, detail="尝试次数过多，请重新获取验证码")

    if verification["code"] != payload.code:
        increment_verify_attempts(verification["id"])
        raise HTTPException(status_code=400, detail="验证码错误")

    result = set_verification_verified(
        verification["id"],
        token_expires_minutes=settings.otp_token_expires_minutes,
    )
    return {"status": "ok", "verified_token": result["verified_token"]}


@app.post("/web/register")
def web_register(payload: WebRegisterRequest) -> dict:
    try:
        phone = normalize_phone(payload.phone)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    verification = get_valid_verification_by_token(
        verified_token=payload.otp_token,
        phone=phone,
    )
    if verification is None:
        raise HTTPException(status_code=400, detail="注册凭证无效或已过期")

    consume_verification_token(verification["id"])

    try:
        platform_user = create_or_get_platform_user_by_phone(
            phone=phone,
            display_name=payload.display_name,
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return {
        "status": "ok",
        "platform_user": platform_user,
        "subscription": get_latest_subscription_for_user(
            platform_user_id=platform_user["id"],
        ),
    }


@app.post("/web/agents")
def web_create_agent(payload: WebCreateAgentRequest) -> dict:
    if get_platform_user(platform_user_id=payload.platform_user_id) is None:
        raise HTTPException(status_code=404, detail="platform_user not found")
    try:
        result = create_ai4all_account_for_user(
            platform_user_id=payload.platform_user_id,
            display_name=payload.agent_name,
            system_prompt=payload.role_prompt,
            plan=payload.plan or "free",
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return {"status": "ok", **result}


@app.post("/web/binding-intents")
def web_create_binding_intent(payload: WebCreateBindingIntentRequest) -> dict:
    try:
        binding_intent = create_binding_intent(
            platform_user_id=payload.platform_user_id,
            account_id=payload.account_id,
            channel=payload.channel or "openclaw-weixin",
        )
        binding_intent = _start_openclaw_qr_for_binding(binding_intent)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return {
        "status": "ok",
        "binding_intent": binding_intent,
        "binding_mode": "openclaw_gateway_qr",
        "next_step": "scan_qr_and_wait_for_completion",
    }


@app.get("/web/binding-intents/{binding_intent_id}")
def web_get_binding_intent(binding_intent_id: str) -> dict:
    binding_intent = get_binding_intent(binding_intent_id=binding_intent_id)
    if binding_intent is None:
        raise HTTPException(status_code=404, detail="binding_intent not found")
    return {"binding_intent": binding_intent}


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
        "channel_bindings": list_channel_bindings_for_account(account_id=account_id),
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
    context = read_agent_context(account_id)
    return {
        "account_id": account_id,
        "path": str(path),
        "content": path.read_text(encoding="utf-8"),
        "agent_context": context.metadata(),
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


@app.post("/admin/accounts/{account_id}/dreaming")
def admin_run_account_dreaming(
    account_id: str,
    days: int = 7,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    return run_dreaming(
        account_id=account_id,
        today=date_cls.today().isoformat(),
        days=days,
    )


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


@app.get("/admin/debug/traces")
def admin_debug_traces(
    account_id: Optional[str] = None,
    session_id: Optional[int] = None,
    limit: int = 50,
    _: None = Depends(verify_admin_auth),
) -> dict:
    return {
        "traces": list_debug_traces(
            account_id=account_id,
            session_id=session_id,
            limit=limit,
        )
    }


@app.get("/admin/debug/traces/{trace_id}")
def admin_debug_trace(
    trace_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    trace = get_debug_trace(trace_id=trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return {"trace": trace}


# ---------------------------------------------------------------------------
# Bridge endpoint
# ---------------------------------------------------------------------------

@app.post("/openclaw/debug-traces")
def openclaw_debug_trace_ingest(
    payload: OpenClawDebugTraceRequest,
    _: None = Depends(verify_bridge_auth),
) -> dict:
    channel_account_id = payload.channel_account_id or payload.account_id
    fallback_session_key = (
        payload.session_key
        or (f"openclaw-debug:{channel_account_id}" if channel_account_id else "openclaw-debug:unknown")
    )
    identity = resolve_openclaw_identity(
        channel=payload.channel or "openclaw",
        session_key=fallback_session_key,
        channel_account_id=channel_account_id,
        sender_id=None,
        chat_id=None,
    )
    account_id = resolve_account_id_for_inbound_channel_identity(
        channel=identity.channel,
        session_key=identity.session_key,
        channel_account_id=identity.channel_account_id,
    )
    session_state = get_or_create_session(
        account_id=account_id,
        channel=identity.channel,
        sender_id=identity.sender_id,
        sender_name=None,
        chat_id=identity.chat_id,
        session_key=identity.session_key,
    )
    binding = upsert_channel_binding(
        account_id=account_id,
        channel=identity.channel,
        session_key=identity.session_key,
        channel_account_id=identity.channel_account_id,
        sender_id=identity.sender_id,
        chat_id=identity.chat_id,
        raw_identity=_identity_response_metadata(identity, account_id),
    )
    session = session_state["session"]
    trace_id = payload.trace_id or f"trace-{uuid.uuid4()}"
    metadata = dict(payload.metadata or {})
    metadata["identity"] = _identity_response_metadata(identity, account_id)
    metadata["channel_binding_id"] = binding["id"]
    inserted_id = insert_debug_trace(
        trace_id=trace_id,
        account_id=account_id,
        session_id=session["id"],
        message_id=payload.message_id,
        source=payload.source or "openclaw",
        llm_model=payload.llm_model,
        system_prompt=payload.system_prompt,
        messages=payload.messages,
        reply=payload.reply,
        metadata=metadata,
        latency_ms=payload.latency_ms,
        error=payload.error,
    )
    return {
        "status": "ok" if inserted_id is not None else "duplicate",
        "trace_id": trace_id,
        "metadata": _identity_response_metadata(identity, account_id),
    }


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

    identity = resolve_openclaw_identity(
        channel=payload.channel,
        session_key=payload.session_key,
        channel_account_id=payload.channel_account_id or payload.account_id,
        sender_id=payload.sender_id,
        chat_id=payload.chat_id,
    )
    session_key = identity.session_key
    account_id = resolve_account_id_for_inbound_channel_identity(
        channel=identity.channel,
        session_key=identity.session_key,
        channel_account_id=identity.channel_account_id,
    )
    sender_id = identity.sender_id
    message_id = payload.message_id or payload.event_id

    session_state = get_or_create_session(
        account_id=account_id,
        channel=identity.channel,
        sender_id=sender_id,
        sender_name=payload.sender_name,
        chat_id=identity.chat_id,
        session_key=session_key,
    )
    binding = upsert_channel_binding(
        account_id=account_id,
        channel=identity.channel,
        session_key=session_key,
        channel_account_id=identity.channel_account_id,
        sender_id=sender_id,
        chat_id=identity.chat_id,
        raw_identity=_identity_response_metadata(identity, account_id),
    )
    account = session_state["account"]
    session = session_state["session"]
    profile_path = ensure_user_profile(account_id)
    debug_trace_enabled = _is_debug_trace_account(account_id)

    if account.get("status") == "disabled":
        logger.info(
            "openclaw_turn ignored disabled account account=%s session=%s",
            account_id,
            session_key,
        )
        return OpenClawTurnResponse(
            status="disabled",
            no_reply=True,
            metadata=_identity_response_metadata(identity, account_id),
        )

    today = date_cls.today().isoformat()
    effective_rpm = settings.rate_limit_rpm if account.get("rpm_limit") is None else account["rpm_limit"]
    effective_daily = settings.rate_limit_daily if account.get("daily_limit") is None else account["daily_limit"]

    if effective_rpm > 0 and not rate_limiter.check_rpm(account_id, effective_rpm):
        logger.info("openclaw_turn rpm_limited account=%s", account_id)
        return OpenClawTurnResponse(
            status="rate_limited",
            reply=settings.rate_limit_rpm_message,
            metadata={**_identity_response_metadata(identity, account_id), "reason": "rpm"},
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
                    **_identity_response_metadata(identity, account_id),
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
            metadata={**_identity_response_metadata(identity, account_id), "latency_ms": latency_ms},
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
            metadata={**_identity_response_metadata(identity, account_id), "latency_ms": latency_ms},
        )

    increment_daily_usage(account_id=account_id, date=today)

    generation_error = None
    system_prompt = None
    llm_messages = []
    debug_metadata = {
        "trace_kind": "ai4all_turn",
        "debug_trace_enabled": debug_trace_enabled,
        "identity": _identity_response_metadata(identity, account_id),
        "channel_binding_id": binding["id"],
        "channel": identity.channel,
        "session_key": session_key,
        "sender_id": sender_id,
        "message_type": payload.message_type,
        "today": today,
    }
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
            soul = extract_section(file_profile, "Soul")
            user_prefs = extract_section(file_profile, "User Preferences")
            long_term_memory = extract_section(file_profile, "Long-term Memory")
            agent_context = read_agent_context(
                account_id,
                display_name=account.get("display_name"),
            )
            daily_notes = read_daily_notes(account_id, today)
            debug_metadata.update(
                {
                    "history_count": len(history),
                    "soul_chars": len(soul),
                    "user_prefs_chars": len(user_prefs),
                    "long_term_memory_chars": len(long_term_memory),
                    "daily_notes_chars": len(daily_notes),
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
                daily_notes=daily_notes,
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
            session_key,
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
        raw={"source": "ai4all"},
        latency_ms=latency_ms,
        error=generation_error,
    )

    if not generation_error and text and text not in _SPECIAL_COMMANDS and _background_loop is not None:
        turns_for_memory = [
            {"role": "user", "content": text},
            {"role": "assistant", "content": reply},
        ]
        _background_loop.call_soon_threadsafe(
            _background_loop.create_task,
            write_memory(account_id=account_id, turns=turns_for_memory, today=today),
        )

    return OpenClawTurnResponse(
        status="ok",
        reply=reply,
        metadata={
            **_identity_response_metadata(identity, account_id),
            "channel_binding_id": binding["id"],
            "message_type": payload.message_type,
            "latency_ms": latency_ms,
            "user_profile_path": str(profile_path),
            "debug_trace_id": trace_id,
        },
    )
