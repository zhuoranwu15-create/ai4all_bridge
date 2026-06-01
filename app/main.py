import asyncio
import logging
import uuid
from datetime import date as date_cls, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import settings
from app.captcha import verify_captcha
from app.sms import generate_otp, send_otp
from app.db import (
    ACCOUNT_ACTIVE_SESSION_KEY,
    clear_session_messages,
    clear_all_messages_for_account,
    cancel_proactive_commitment,
    consume_valid_verification_token,
    count_verifications_last_hour,
    create_ai4all_account_for_user,
    create_admin_plaintext_grant,
    create_binding_intent,
    create_search_provider_run,
    create_tool_invocation,
    create_or_get_platform_user_by_phone,
    create_phone_verification,
    find_active_admin_plaintext_grant,
    get_admin_plaintext_grant,
    get_debug_trace,
    get_dreaming_memory_item,
    get_dreaming_run,
    get_account,
    get_account_onboarding_state,
    get_admin_user as get_admin_user_record,
    get_binding_intent,
    get_daily_usage,
    get_latest_active_verification,
    get_latest_subscription_for_user,
    get_message_raw,
    get_or_create_default_ai4all_account_for_user,
    get_platform_user,
    get_profile_for_account,
    get_profile_for_session,
    get_proactive_commitment,
    get_session,
    get_or_create_session,
    get_usage_last_7_days,
    get_wallet_summary,
    increment_verify_attempts,
    init_db,
    insert_admin_access_event,
    insert_debug_trace,
    invalidate_verifications_for_phone,
    list_accounts,
    list_admin_access_events,
    list_admin_plaintext_grants,
    list_admin_users,
    list_account_owner_bindings_for_account,
    list_binding_intents_for_account,
    list_content_invitations_for_account,
    list_outbound_messages,
    list_proactive_commitments_for_account,
    list_debug_traces,
    list_dreaming_memory_items,
    list_dreaming_runs,
    list_memory_events,
    list_recent_message_raw,
    list_session_messages,
    list_sessions,
    list_sessions_for_account,
    create_platform_user_session,
    get_platform_user_by_session_token,
    normalize_phone,
    resolve_account_id_for_inbound_channel_identity,
    set_account_onboarding_state,
    set_binding_intent_error,
    set_account_status,
    set_verification_verified,
    update_admin_plaintext_grant_status,
    update_binding_intent,
    update_account,
    update_profile_for_account,
    update_profile_for_session,
    get_proactive_account_state,
    list_channel_bindings_for_account,
    upsert_proactive_account_state,
    upsert_channel_binding,
    upsert_admin_user,
    cancel_reminder,
    get_reminder,
    list_reminders_for_account,
    get_tool_invocation,
    list_wallet_ledger,
    list_search_provider_runs,
    list_tool_invocations,
    update_reminder,
    update_tool_invocation,
    unbind_account_channel,
    wipe_account_data,
    reenable_proactive_after_rebind,
)
from app.identity import identity_response_metadata, resolve_openclaw_identity
from app.onboarding import ONBOARDING_STEP1_SENT, ONBOARDING_WELCOME_TEXT
from app.openclaw_gateway import (
    logout_weixin_account,
    send_weixin_text,
    start_weixin_qr_login,
    wait_weixin_qr_login,
)
from app.dreaming_scheduler import (
    get_dreaming_scheduler,
    run_dreaming_scheduler_once,
    start_dreaming_scheduler,
    stop_dreaming_scheduler,
)
from app.prompt_builder import PromptBuilder, extract_section
from app.proactive.scheduler import (
    get_proactive_scheduler,
    run_proactive_scheduler_once,
    start_proactive_scheduler,
    stop_proactive_scheduler,
)
from app.proactive.account_checks import (
    clear_account_check_candidate_draft,
    decide_account_check_action,
    execute_account_check_decision,
    generate_content_invitation_candidate,
    generate_account_check_candidate_draft,
    promote_account_check_candidate_draft,
)
from app.proactive.state import format_state_time
from app.schemas import OpenClawDebugTraceRequest, OpenClawTurnRequest, OpenClawTurnResponse
from app.dreaming import (
    rollback_memory_item,
    run_dreaming,
    summarize_dreaming_run_for_debug,
    summarize_memory_item_for_debug,
)
from app.session_lifecycle import run_daily_dreaming_scan
from app.turn_service import handle_openclaw_turn
from app.tools import get_web_search_tools
from app.tools.web_search_handlers import handle_web_search, override_provider_order
import shutil

from app.user_profiles import ensure_user_profile, read_user_profile, read_agent_context, account_profile_dir


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ai4all")

_background_loop: Optional[asyncio.AbstractEventLoop] = None

app = FastAPI(title="AI4ALL Weixin Bot", version="0.1.0")

_LOCAL_DEBUG_UI_ENVS = {"local", "development", "test"}
_LOCAL_ONLY_DEBUG_UI_PATHS = {
    "/ui/onboarding_debug.html",
    "/ui/proactive_debug.html",
    "/ui/web_search_debug.html",
}
_WEB_SEARCH_DEBUG_PROVIDERS = {"aliyun", "baidu", "bing", "duckduckgo"}


@app.middleware("http")
async def _gate_debug_ui(request: Request, call_next):
    if (
        request.url.path in _LOCAL_ONLY_DEBUG_UI_PATHS
        and str(settings.app_env or "").lower() not in _LOCAL_DEBUG_UI_ENVS
    ):
        return JSONResponse({"detail": "Not available"}, status_code=403)
    return await call_next(request)


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


class ProactiveAccountStateUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    next_scan_at: Optional[str] = None
    cooldown_until: Optional[str] = None
    metadata: Optional[dict[str, Any]] = Field(default=None)


class PlaintextGrantRequest(BaseModel):
    reason: str
    account_scope: list[str] = Field(default_factory=list)
    resource_scope: list[str] = Field(default_factory=list)
    time_scope_start: Optional[str] = None
    time_scope_end: Optional[str] = None


class WebRegisterRequest(BaseModel):
    phone: str
    display_name: Optional[str] = None
    otp_token: str


class WebRegisterAndBindingIntentRequest(BaseModel):
    phone: str
    display_name: Optional[str] = None
    otp_token: str
    channel: Optional[str] = "openclaw-weixin"


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
    channel: Optional[str] = "openclaw-weixin"


class WebUnbindRequest(BaseModel):
    keep_memories: bool


def _debug_trace_account_ids() -> set[str]:
    raw = getattr(settings, "debug_trace_account_ids", "") or ""
    return {item.strip() for item in raw.split(",") if item.strip()}


def _is_debug_trace_account(account_id: str) -> bool:
    return account_id in _debug_trace_account_ids()


def _normalize_openclaw_weixin_account_id(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    if "openclaw-weixin:" in text:
        text = text.split("openclaw-weixin:", 1)[1].split(":", 1)[0].strip()
    if text.endswith("@im.bot"):
        return f"{text[:-7]}-im-bot"
    if text.endswith("-im-bot"):
        return text
    if text.endswith("@im.wechat"):
        return f"{text[:-10]}-im-wechat"
    if text.endswith("-im-wechat"):
        return text
    return None


def _collect_openclaw_weixin_logout_targets(bindings: list[dict]) -> list[str]:
    targets = []
    seen = set()
    for binding in bindings:
        if binding.get("channel") != "openclaw-weixin":
            continue
        raw_identity = binding.get("raw_identity") or {}
        candidates = [
            binding.get("channel_account_id"),
            binding.get("session_key"),
            raw_identity.get("channel_account_id"),
            raw_identity.get("accountId"),
            raw_identity.get("account_id"),
            raw_identity.get("session_key"),
            raw_identity.get("openclaw_session_key_account_id"),
        ]
        for candidate in candidates:
            target = _normalize_openclaw_weixin_account_id(candidate)
            if target and target not in seen:
                targets.append(target)
                seen.add(target)
    return targets


def _cleanup_openclaw_weixin_accounts(bindings: list[dict]) -> dict:
    targets = _collect_openclaw_weixin_logout_targets(bindings)
    if not targets:
        return {
            "status": "skipped",
            "reason": "no_openclaw_weixin_binding",
            "attempts": [],
        }

    attempts = []
    for target in targets:
        try:
            result = logout_weixin_account(
                account_id=target,
                channel="openclaw-weixin",
                timeout_ms=settings.openclaw_gateway_call_timeout_ms,
            )
            attempts.append(
                {
                    "channel": "openclaw-weixin",
                    "account_id": target,
                    "status": "ok",
                    "result": result,
                }
            )
        except Exception as err:
            error = str(err)
            status = "unsupported" if "does not support logout" in error else "failed"
            logger.warning("OpenClaw Weixin logout failed account=%s error=%s", target, error)
            attempts.append(
                {
                    "channel": "openclaw-weixin",
                    "account_id": target,
                    "status": status,
                    "error": error,
                }
            )

    if all(item["status"] == "ok" for item in attempts):
        status = "ok"
    elif all(item["status"] == "unsupported" for item in attempts):
        status = "unsupported"
    elif any(item["status"] == "ok" for item in attempts):
        status = "partial_failed"
    else:
        status = "failed"
    return {"status": status, "attempts": attempts}


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
        # sender_id (WeChat OpenID) is not available at binding time — it only
        # arrives with the user's first inbound message. Proactive welcome is
        # skipped until then (see _send_onboarding_proactive_welcome).
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
        reenable_proactive_after_rebind(account_id=binding_intent["account_id"])
        return

    if result.get("alreadyConnected") and channel_account_id:
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
                "already_connected": True,
            },
        )
        reenable_proactive_after_rebind(account_id=binding_intent["account_id"])
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

    # After successful binding, schedule a 5-second proactive onboarding welcome.
    latest_after = get_binding_intent(binding_intent_id=binding_intent_id)
    if latest_after and latest_after.get("status") == "completed":
        account_id = latest_after["account_id"]
        channel = latest_after["channel"]
        channel_account_id = latest_after.get("channel_account_id")
        session_key = latest_after.get("openclaw_login_session_key")
        loop = asyncio.get_event_loop()
        loop.call_later(
            5.0,
            lambda: loop.create_task(
                _send_onboarding_welcome_if_pending(
                    account_id=account_id,
                    channel=channel,
                    channel_account_id=channel_account_id,
                    session_key=session_key,
                )
            ),
        )


_ONBOARDING_WELCOME_TEXT = ONBOARDING_WELCOME_TEXT


async def _send_onboarding_welcome_if_pending(
    *,
    account_id: str,
    channel: str,
    channel_account_id: Optional[str],
    session_key: Optional[str],
) -> None:
    """Send the onboarding welcome message if the user hasn't sent their first message yet.

    Best-effort: requires a sender_id from channel_bindings. If not available,
    the user will trigger onboarding with their first inbound message instead.
    """
    try:
        state = await asyncio.to_thread(
            get_account_onboarding_state, account_id=account_id
        )
        if state != "pending":
            logger.info(
                "onboarding proactive skipped account=%s state=%s (already advanced)",
                account_id, state,
            )
            return

        # Look up the sendable peer from channel bindings (populated when user first messages)
        bindings = await asyncio.to_thread(
            list_channel_bindings_for_account, account_id=account_id
        )
        to_user_id = None
        resolved_session_key = session_key
        for b in bindings:
            peer = b.get("chat_id") or b.get("sender_id")
            if peer:
                to_user_id = peer
                resolved_session_key = b.get("session_key") or session_key
                break

        if not to_user_id:
            logger.info(
                "onboarding proactive skipped account=%s reason=no_weixin_peer_yet "
                "(user will trigger onboarding with first inbound message)",
                account_id,
            )
            return

        await asyncio.to_thread(
            send_weixin_text,
            to_user_id=to_user_id,
            text=_ONBOARDING_WELCOME_TEXT,
            gateway_timeout_ms=settings.openclaw_gateway_call_timeout_ms,
            account_id=channel_account_id,
            session_key=resolved_session_key,
            channel=channel,
        )
        await asyncio.to_thread(
            set_account_onboarding_state, account_id=account_id, state=ONBOARDING_STEP1_SENT
        )
        logger.info(
            "onboarding proactive welcome sent account=%s to=%s state->step1_sent",
            account_id, to_user_id,
        )
    except Exception as err:
        logger.exception(
            "onboarding proactive welcome failed account=%s error=%s", account_id, err
        )


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


@app.on_event("startup")
async def startup_proactive_scheduler() -> None:
    if not getattr(settings, "proactive_scheduler_enabled", False):
        return
    scheduler = start_proactive_scheduler(
        interval_seconds=settings.proactive_scheduler_interval_seconds,
        batch_size=settings.proactive_scheduler_batch_size,
        bypass_quiet_hours=settings.proactive_scheduler_bypass_quiet_hours,
        account_check_interval_seconds=settings.proactive_account_check_interval_seconds,
    )
    logger.info("proactive scheduler started: %s", scheduler.status())


@app.on_event("startup")
async def startup_dreaming_scheduler() -> None:
    if not getattr(settings, "dreaming_scheduler_enabled", False):
        return
    scheduler = start_dreaming_scheduler(
        interval_seconds=settings.dreaming_scheduler_interval_seconds,
        batch_size=settings.dreaming_scheduler_batch_size,
    )
    logger.info("dreaming scheduler started: %s", scheduler.status())


@app.on_event("shutdown")
async def shutdown_proactive_scheduler() -> None:
    await stop_proactive_scheduler()


@app.on_event("shutdown")
async def shutdown_dreaming_scheduler() -> None:
    await stop_dreaming_scheduler()


def verify_bridge_auth(authorization: Optional[str] = Header(default=None)) -> None:
    expected = f"Bearer {settings.ai4all_bridge_secret}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bridge authorization",
        )


def get_admin_user(authorization: Optional[str] = Header(default=None)) -> dict:
    admin_expected = f"Bearer {settings.admin_token}"
    if authorization == admin_expected:
        user = upsert_admin_user(
            admin_user_id="admin",
            role="admin",
            display_name="Admin",
        )
        return user

    staff_token = str(getattr(settings, "admin_staff_token", "") or "").strip()
    if staff_token and authorization == f"Bearer {staff_token}":
        user = upsert_admin_user(
            admin_user_id="staff",
            role="staff",
            display_name="Staff",
        )
        return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid admin authorization",
    )


def verify_admin_auth(admin_user: dict = Depends(get_admin_user)) -> None:
    return None


def require_admin_user(admin_user: dict = Depends(get_admin_user)) -> dict:
    if admin_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin role required",
        )
    return admin_user


def _normalize_optional_state_datetime(value: Optional[str], *, field_name: str) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("T", " ")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be an ISO datetime or YYYY-MM-DD HH:MM:SS",
        )
    return format_state_time(parsed)


_REDACTED_TEXT_FIELDS = {
    "content",
    "text",
    "reply",
    "system_prompt",
    "prompt",
    "messages",
    "raw_payload",
    "message",
    "description",
    "caption",
}
_METADATA_TEXT_FIELDS = {
    "source",
    "channel",
    "channel_account_id",
    "openclaw_session_key",
    "account_active_session_key",
    "sender_id",
    "chat_id",
    "message_id",
    "message_type",
    "reply_to_message_id",
    "trace_kind",
    "mode",
    "identity",
    "ai4all_bridge",
    "raw_keys",
}


def _content_meta(value: Any) -> dict:
    text = "" if value is None else str(value)
    return {
        "redacted": True,
        "chars": len(text),
        "preview": None,
    }


def _redact_raw_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.lower()
            if (
                normalized in _REDACTED_TEXT_FIELDS
                or normalized.endswith(("_content", "_text", "_prompt", "_reply"))
                or normalized.startswith(("content_", "text_", "prompt_", "reply_"))
            ):
                redacted[key_text] = _content_meta(item)
            elif isinstance(item, (dict, list)):
                redacted[key_text] = _redact_raw_payload(item)
            else:
                redacted[key_text] = item
        return redacted
    if isinstance(value, list):
        return [_redact_raw_payload(item) for item in value]
    return value


def _redact_message(message: dict) -> dict:
    item = dict(message)
    content = item.pop("content", None)
    item["content_redacted"] = True
    item["content_chars"] = len(content or "")
    item["content_preview"] = None
    if "raw" in item:
        item["raw_redacted"] = True
        item["raw"] = _redact_raw_payload(item.get("raw") or {})
    return item


def _redact_trace(trace: dict) -> dict:
    item = dict(trace)
    system_prompt = item.pop("system_prompt", None)
    messages = item.pop("messages", None)
    reply = item.pop("reply", None)
    item["system_prompt_redacted"] = True
    item["system_prompt_chars"] = len(system_prompt or "")
    item["messages_redacted"] = True
    item["messages_count"] = len(messages or []) if isinstance(messages, list) else 0
    item["messages_chars"] = sum(
        len(str(message.get("content") or ""))
        for message in messages or []
        if isinstance(message, dict)
    )
    item["reply_redacted"] = True
    item["reply_chars"] = len(reply or "")
    if "metadata" in item:
        item["metadata"] = _redact_raw_payload(item.get("metadata") or {})
    return item


def _redact_profile(profile: dict) -> dict:
    item = dict(profile or {})
    if "system_prompt" in item:
        system_prompt = item.pop("system_prompt")
        item["system_prompt_redacted"] = True
        item["system_prompt_chars"] = len(system_prompt or "")
    if "preferences_json" in item:
        prefs = item.pop("preferences_json")
        item["preferences_redacted"] = True
        item["preferences_chars"] = len(prefs or "")
    return item


def _redact_session(session: dict) -> dict:
    item = dict(session or {})
    for field in ("session_summary", "carryover_summary"):
        if field in item:
            value = item.pop(field)
            item[f"{field}_redacted"] = True
            item[f"{field}_chars"] = len(value or "")
    return item


def _redact_text_field(item: dict, field: str = "text") -> dict:
    redacted = dict(item or {})
    value = redacted.pop(field, None)
    redacted[f"{field}_redacted"] = True
    redacted[f"{field}_chars"] = len(value or "")
    redacted[f"{field}_preview"] = None
    if "metadata" in redacted:
        redacted["metadata"] = _redact_raw_payload(redacted.get("metadata") or {})
    return redacted


def _proactive_state_for_overview(state: Optional[dict]) -> Optional[dict]:
    if state is None:
        return None
    item = dict(state)
    if "metadata" in item:
        item["metadata"] = _redact_raw_payload(item.get("metadata") or {})
    return item


def _content_invitation_for_overview(invitation: dict) -> dict:
    item = dict(invitation or {})
    invitation_text = item.pop("invitation_text", None)
    titles = item.pop("title_items", None) or []
    item["invitation_text_redacted"] = True
    item["invitation_text_chars"] = len(invitation_text or "")
    item["title_count"] = len(titles)
    if "metadata" in item:
        item["metadata"] = _redact_raw_payload(item.get("metadata") or {})
    return item


def _redact_phone(phone: Optional[str]) -> Optional[str]:
    text = str(phone or "").strip()
    if not text:
        return None
    if len(text) <= 4:
        return "*" * len(text)
    return "*" * max(0, len(text) - 4) + text[-4:]


def _redact_platform_user(user: Optional[dict]) -> Optional[dict]:
    if user is None:
        return None
    item = dict(user)
    item["phone_redacted"] = True
    item["phone_last4"] = str(item.get("phone") or "")[-4:] if item.get("phone") else None
    item["phone"] = _redact_phone(item.get("phone"))
    return item


def _binding_intent_for_view(intent: dict, *, account_id: Optional[str]) -> dict:
    item = dict(intent or {})
    if _can_bypass_redaction_for_account(account_id):
        item["plaintext_debug"] = True
        return item
    qr_data_url = item.pop("qr_data_url", None)
    manual_login_command = item.pop("manual_login_command", None)
    raw_result = item.pop("raw_result", None)
    item["qr_data_url_redacted"] = bool(qr_data_url)
    item["manual_login_command_redacted"] = bool(manual_login_command)
    item["raw_result_redacted"] = bool(raw_result)
    if isinstance(raw_result, dict):
        item["raw_result_keys"] = sorted(str(key) for key in raw_result.keys())
    return item


def _debug_plaintext_account_allowlist() -> set[str]:
    raw = getattr(settings, "admin_debug_plaintext_account_allowlist", "") or ""
    return {value.strip() for value in raw.split(",") if value.strip()}


def _is_non_production_env() -> bool:
    return str(getattr(settings, "app_env", "") or "").lower() in {"local", "development", "test"}


def _can_bypass_redaction_for_account(account_id: Optional[str]) -> bool:
    if not account_id:
        return False
    if account_id in _debug_plaintext_account_allowlist():
        return True
    if bool(getattr(settings, "admin_debug_plaintext_enabled", False)):
        return _is_non_production_env()
    return False


def _message_plaintext(message: dict) -> dict:
    item = dict(message)
    item["plaintext_debug"] = True
    return item


def _trace_plaintext(trace: dict) -> dict:
    item = dict(trace)
    item["plaintext_debug"] = True
    return item


def _profile_plaintext(profile: dict) -> dict:
    item = dict(profile or {})
    item["plaintext_debug"] = True
    return item


def _session_plaintext(session: dict) -> dict:
    item = dict(session or {})
    item["plaintext_debug"] = True
    return item


def _debug_redaction_payload(*, account_id: Optional[str] = None, source: str = "admin_debug") -> dict:
    return {
        "redacted": False,
        "plaintext_debug": True,
        "plaintext_debug_source": source,
        "plaintext_debug_account_id": account_id,
    }


def _session_for_view(session: dict) -> dict:
    account_id = session.get("account_id") if session else None
    if _can_bypass_redaction_for_account(account_id):
        return _session_plaintext(session)
    return _redact_session(session)


def _profile_for_view(profile: dict, *, account_id: Optional[str]) -> dict:
    if _can_bypass_redaction_for_account(account_id):
        return _profile_plaintext(profile)
    return _redact_profile(profile)


def _platform_user_for_view(user: Optional[dict], *, account_id: Optional[str]) -> Optional[dict]:
    if user is None:
        return None
    if _can_bypass_redaction_for_account(account_id):
        item = dict(user)
        item["plaintext_debug"] = True
        return item
    return _redact_platform_user(user)


def _message_for_view(message: dict, *, account_id: Optional[str] = None) -> dict:
    target_account_id = account_id or message.get("account_id")
    if _can_bypass_redaction_for_account(target_account_id):
        return _message_plaintext(message)
    return _redact_message(message)


def _trace_for_view(trace: dict) -> dict:
    if _can_bypass_redaction_for_account(trace.get("account_id")):
        return _trace_plaintext(trace)
    return _redact_trace(trace)


def _redacted_flag_for_account(account_id: Optional[str]) -> bool:
    return not _can_bypass_redaction_for_account(account_id)


def _audit_plaintext_access(
    *,
    admin_user: dict,
    action: str,
    resource_type: str,
    resource_id: Optional[str],
    account_id: Optional[str],
    request_path: str,
    reason: Optional[str] = None,
    grant_id: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> None:
    insert_admin_access_event(
        admin_user_id=admin_user.get("id"),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        account_id=account_id,
        plaintext=True,
        grant_id=grant_id,
        reason=reason or "admin_plaintext_access",
        request_path=request_path,
        metadata=metadata,
    )


def _now_db_time() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _require_plaintext_access(
    *,
    admin_user: dict,
    account_id: str,
    resource_type: str,
) -> Optional[dict]:
    if admin_user.get("role") == "admin":
        return None
    grant = find_active_admin_plaintext_grant(
        requester_admin_user_id=str(admin_user.get("id")),
        account_id=account_id,
        resource_type=resource_type,
        now=_now_db_time(),
    )
    if grant is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="plaintext grant required",
        )
    return grant


def _clean_scope_list(values: list[str], *, field_name: str) -> list[str]:
    result = []
    for value in values or []:
        text = str(value or "").strip()
        if text:
            result.append(text)
    if not result:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must include at least one value",
        )
    return list(dict.fromkeys(result))


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "env": settings.app_env}


@app.get("/admin/me")
def admin_me(admin_user: dict = Depends(get_admin_user)) -> dict:
    return {"admin_user": admin_user}


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
    account_id = session.get("account_id")
    redacted = _redacted_flag_for_account(account_id)
    return {
        "session": _session_for_view(session),
        "profile": _profile_for_view(
            get_profile_for_session(session_id=session_id) or {},
            account_id=account_id,
        ),
        "messages": [
            _message_for_view(message, account_id=account_id)
            for message in list_session_messages(session_id=session_id, limit=limit)
        ],
        **(_debug_redaction_payload(account_id=account_id) if not redacted else {"redacted": True}),
    }


@app.get("/debug/messages/raw")
def debug_recent_message_raw(limit: int = 20, _: None = Depends(verify_admin_auth)) -> dict:
    messages = list_recent_message_raw(limit=limit)
    return {
        "messages": [
            _message_for_view(message)
            for message in messages
        ],
        "redacted": not any(_can_bypass_redaction_for_account(message.get("account_id")) for message in messages),
    }


@app.get("/debug/messages/{message_db_id}/raw")
def debug_message_raw(message_db_id: int, _: None = Depends(verify_admin_auth)) -> dict:
    message = get_message_raw(message_db_id=message_db_id)
    if message is None:
        raise HTTPException(status_code=404, detail="message not found")
    account_id = message.get("account_id")
    redacted = _redacted_flag_for_account(account_id)
    return {
        "message": _message_for_view(message),
        **(_debug_redaction_payload(account_id=account_id) if not redacted else {"redacted": True}),
    }


@app.get("/debug/traces")
def debug_traces(
    account_id: Optional[str] = None,
    session_id: Optional[int] = None,
    limit: int = 50,
    _: None = Depends(verify_admin_auth),
) -> dict:
    return {
        "traces": [
            _trace_for_view(t) for t in list_debug_traces(
                account_id=account_id,
                session_id=session_id,
                limit=limit,
            )
        ]
    }


@app.get("/debug/traces/{trace_id}")
def debug_trace(trace_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    trace = get_debug_trace(trace_id=trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    account_id = trace.get("account_id")
    redacted = _redacted_flag_for_account(account_id)
    return {
        "trace": _trace_for_view(trace),
        **(_debug_redaction_payload(account_id=account_id) if not redacted else {"redacted": True}),
    }


@app.get("/debug/accounts/{account_id}/prompt-preview")
def debug_prompt_preview(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    """Show the assembled system prompt and per-block sizes for an account."""
    from app.user_profiles import read_user_profile, read_agent_context
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
    builder = PromptBuilder()
    prompt = builder.build(
        display_name=account.get("display_name"),
        soul=soul,
        user_prefs=user_prefs,
        long_term_memory=long_term_memory,
        daily_notes=None,
        system_prompt_override=profile.get("system_prompt"),
        style=profile.get("style"),
        agent_context=agent_context.blocks,
        today=today,
        model_name=settings.llm_model,
    )
    if _can_bypass_redaction_for_account(account_id):
        return {
            "account_id": account_id,
            "today": today,
            "total_chars": len(prompt),
            "blocks": {
                "soul_chars": len(soul),
                "user_prefs_chars": len(user_prefs),
                "long_term_memory_chars": len(long_term_memory),
                "daily_notes_loaded": False,
                "daily_notes_chars": 0,
                "system_prompt_override": bool(profile.get("system_prompt")),
                "style": profile.get("style"),
                "display_name": account.get("display_name"),
                "agent_context": agent_context.metadata(),
            },
            "prompt": prompt,
            **_debug_redaction_payload(account_id=account_id),
        }
    return {
        "account_id": account_id,
        "today": today,
        "total_chars": len(prompt),
        "blocks": {
            "soul_chars": len(soul),
            "user_prefs_chars": len(user_prefs),
            "long_term_memory_chars": len(long_term_memory),
            "daily_notes_loaded": False,
            "daily_notes_chars": 0,
            "system_prompt_override": bool(profile.get("system_prompt")),
            "style": profile.get("style"),
            "display_name": account.get("display_name"),
            "agent_context": agent_context.metadata(),
        },
        "prompt_redacted": True,
        "prompt_chars": len(prompt),
        "redacted": True,
    }


@app.get("/debug/accounts/{account_id}/user-profile")
def debug_get_user_profile(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    path = ensure_user_profile(account_id)
    context = read_agent_context(account_id)
    content = path.read_text(encoding="utf-8")
    if _can_bypass_redaction_for_account(account_id):
        return {
            "account_id": account_id,
            "path": str(path),
            "content": content,
            "agent_context": context.metadata(),
            **_debug_redaction_payload(account_id=account_id),
        }
    return {
        "account_id": account_id,
        "path": str(path),
        "content_redacted": True,
        "content_chars": len(content),
        "agent_context": context.metadata(),
        "redacted": True,
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


@app.get("/debug/accounts/{account_id}/onboarding")
def debug_get_onboarding(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    """Return onboarding state and collected context file contents for an account."""
    from app.onboarding import build_onboarding_prompt_context, is_onboarding_active
    from app.user_profiles import read_agent_context, context_file_path, CONTEXT_FILE_ORDER
    import re

    account = get_account(account_id=account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")

    state = get_account_onboarding_state(account_id=account_id)
    agent_ctx = read_agent_context(account_id)

    identity_text = agent_ctx.blocks.get("IDENTITY", "")
    user_text = agent_ctx.blocks.get("USER", "")
    soul_text = agent_ctx.blocks.get("SOUL", "")

    ai_name: Optional[str] = None
    m = re.search(r"AI 名字[:：]\s*(.+)", identity_text)
    if m:
        ai_name = m.group(1).strip()

    user_name: Optional[str] = None
    m = re.search(r"用户称呼[:：]\s*(.+)", user_text)
    if m:
        user_name = m.group(1).strip()

    prompt_ctx = build_onboarding_prompt_context(
        state=state,
        user_name=user_name,
        ai_name=ai_name,
        persona=None,
        user_name_ask_count=0,
        persona_ask_count=0,
    )

    return {
        "account_id": account_id,
        "onboarding_state": state,
        "onboarding_active": is_onboarding_active(state),
        "collected": {
            "user_name": user_name,
            "ai_name": ai_name,
            "soul_chars": len(soul_text),
            "soul_preview": soul_text[:200] if soul_text else None,
        },
        "context_files": {
            filename: {
                "exists": (context_file_path(account_id, filename)).exists(),
                "chars": agent_ctx.files.get(filename, {}).get("chars", 0),
            }
            for filename in CONTEXT_FILE_ORDER
        },
        "prompt_context_preview": prompt_ctx[:500] if prompt_ctx else None,
    }


class OnboardingStateUpdateRequest(BaseModel):
    state: str


@app.patch("/debug/accounts/{account_id}/onboarding/state")
def debug_set_onboarding_state(
    account_id: str,
    payload: OnboardingStateUpdateRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    """Manually set onboarding state — useful for testing specific steps."""
    from app.onboarding import ONBOARDING_COMPLETE, ONBOARDING_TIMED_OUT
    valid_states = {"pending", "step1_sent", "step2_sent", "step3_sent", "complete", "timed_out"}
    if payload.state not in valid_states:
        raise HTTPException(status_code=400, detail=f"invalid state, must be one of: {sorted(valid_states)}")
    account = get_account(account_id=account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    set_account_onboarding_state(account_id=account_id, state=payload.state)
    return {"status": "ok", "account_id": account_id, "onboarding_state": payload.state}


class OnboardingResetRequest(BaseModel):
    clear_context_files: bool = True


@app.post("/debug/accounts/{account_id}/onboarding/reset")
def debug_reset_onboarding(
    account_id: str,
    payload: OnboardingResetRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    """Reset onboarding to pending. Optionally wipe SOUL.md / IDENTITY.md / USER.md.

    Safe to call multiple times. Useful for re-testing the full onboarding flow
    without needing to re-bind a WeChat account.
    """
    import shutil
    from app.user_profiles import account_profile_dir, context_file_path, ensure_agent_context_files

    account = get_account(account_id=account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")

    set_account_onboarding_state(account_id=account_id, state="pending")

    # Clear all session messages so LLM starts fresh without prior conversation history
    cleared_messages = clear_all_messages_for_account(account_id=account_id)

    cleared = []
    if payload.clear_context_files:
        for filename in ("SOUL.md", "IDENTITY.md", "USER.md"):
            path = context_file_path(account_id, filename)
            if path.exists():
                path.unlink()
                cleared.append(filename)
        # Clear daily memory notes (memory/YYYY-MM-DD.md files)
        memory_dir = account_profile_dir(account_id) / "memory"
        if memory_dir.exists():
            shutil.rmtree(memory_dir)
            cleared.append("memory/")
        # Re-create defaults
        ensure_agent_context_files(account_id, display_name=account.get("display_name"))

    return {
        "status": "ok",
        "account_id": account_id,
        "onboarding_state": "pending",
        "cleared_files": cleared,
        "cleared_messages": cleared_messages,
    }


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
# Reminder debug routes
# ---------------------------------------------------------------------------

class ReminderDebugUpdateRequest(BaseModel):
    text: Optional[str] = None
    due_at: Optional[str] = None
    recur_rule: Optional[str] = None
    clear_recur_rule: bool = False


@app.get("/debug/reminders/{account_id}")
def debug_get_reminders(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    reminders = list_reminders_for_account(account_id=account_id, limit=100)
    return {"account_id": account_id, "reminders": reminders}


@app.patch("/debug/reminders/{reminder_id}")
def debug_patch_reminder(
    reminder_id: str,
    payload: ReminderDebugUpdateRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    reminder = get_reminder(reminder_id=reminder_id)
    if reminder is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    if reminder["status"] != "pending":
        raise HTTPException(status_code=400, detail="only pending reminders can be edited")
    updated = update_reminder(
        reminder_id=reminder_id,
        text=payload.text,
        due_at=payload.due_at,
        recur_rule=payload.recur_rule,
        clear_recur_rule=payload.clear_recur_rule,
    )
    return {"status": "ok", "reminder": updated}


@app.delete("/debug/reminders/{reminder_id}")
def debug_delete_reminder(reminder_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    reminder = get_reminder(reminder_id=reminder_id)
    if reminder is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    if reminder["status"] != "pending":
        raise HTTPException(status_code=400, detail="only pending reminders can be cancelled")
    cancelled = cancel_reminder(reminder_id=reminder_id)
    return {"status": "ok", "reminder": cancelled}


# ---------------------------------------------------------------------------
# Web Search debug routes
# ---------------------------------------------------------------------------

class WebSearchDebugSimulationRequest(BaseModel):
    query: str
    provider: Optional[str] = None
    status: str = "succeeded"
    count: int = Field(default=3, ge=1, le=10)
    latency_ms: Optional[int] = Field(default=None, ge=0)
    error: Optional[str] = None


class WebSearchDebugChatRequest(BaseModel):
    text: str
    force_web_search_enabled: bool = True
    provider: Optional[str] = None


class WebSearchDebugRunRequest(BaseModel):
    query: str
    provider: Optional[str] = None
    count: int = Field(default=3, ge=1, le=10)
    language: Optional[str] = None
    country: Optional[str] = None
    freshness: Optional[str] = None
    date_after: Optional[str] = None
    date_before: Optional[str] = None


def _web_search_debug_capabilities() -> dict:
    default_provider = getattr(settings, "web_search_default_provider", "duckduckgo")
    provider_order = [
        item.strip()
        for item in str(getattr(settings, "web_search_provider_order", "") or default_provider).split(",")
        if item.strip()
    ]
    configured_providers = {
        "aliyun": bool(
            getattr(settings, "aliyun_web_search_enabled", False)
            and (
                getattr(settings, "aliyun_web_search_api_key", "")
                or getattr(settings, "dashscope_api_key", "")
            )
        ),
        "baidu": bool(getattr(settings, "baidu_ai_search_enabled", False) and getattr(settings, "baidu_ai_search_api_key", "")),
        "bing": True,
        "duckduckgo": True,
    }
    return {
        "tool_schema_defined": True,
        "model_exposure_configured": bool(getattr(settings, "web_search_enabled", False)),
        "currently_in_turn_tools": bool(getattr(settings, "web_search_enabled", False)),
        "debug_chat_forces_tool_exposure": True,
        "provider_adapter_ready": any(configured_providers.get(provider, False) for provider in provider_order),
        "default_provider": default_provider,
        "provider_order": provider_order,
        "provider_failover": bool(getattr(settings, "web_search_provider_failover", True)),
        "configured_providers": configured_providers,
        "sync_timeout_seconds": getattr(settings, "web_search_sync_timeout_seconds", 8.0),
        "max_results": getattr(settings, "web_search_max_results", 5),
    }


def _web_search_debug_conversation(account_id: str, *, limit: int = 100) -> dict:
    sessions = list_sessions_for_account(account_id=account_id, limit=20)
    active_session = next(
        (session for session in sessions if session.get("session_key") == ACCOUNT_ACTIVE_SESSION_KEY),
        None,
    )
    messages = []
    if active_session is not None:
        messages = list_session_messages(session_id=int(active_session["id"]), limit=limit)
    return {
        "session": active_session,
        "messages": messages,
    }


def _fake_web_search_results(*, query: str, count: int) -> list[dict]:
    return [
        {
            "title": f"Debug result {idx + 1}: {query}",
            "url": f"https://example.com/search-debug/{idx + 1}",
            "snippet": "This is a synthetic web_search debug result. No external provider was called.",
            "site_name": "example.com",
            "retrieved_at": datetime.now().isoformat(timespec="seconds"),
            "score": round(1.0 - idx * 0.08, 2),
        }
        for idx in range(count)
    ]


def _debug_provider_override(provider: Optional[str]) -> Optional[list[str]]:
    cleaned = str(provider or "").strip().lower()
    if cleaned and cleaned not in _WEB_SEARCH_DEBUG_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"unsupported web_search provider: {cleaned}")
    return [cleaned] if cleaned else None


@app.get("/debug/web-search/{account_id}")
def debug_get_web_search(
    account_id: str,
    limit: int = 50,
    _: None = Depends(verify_admin_auth),
) -> dict:
    return {
        "account_id": account_id,
        "capabilities": _web_search_debug_capabilities(),
        "tool_schema": get_web_search_tools()[0],
        "tool_invocations": list_tool_invocations(
            account_id=account_id,
            tool_name="web_search",
            limit=limit,
        ),
        "provider_runs": list_search_provider_runs(
            account_id=account_id,
            limit=limit,
        ),
        "conversation": _web_search_debug_conversation(account_id),
    }


@app.get("/debug/web-search/invocations/{tool_invocation_id}")
def debug_get_web_search_invocation(
    tool_invocation_id: int,
    _: None = Depends(verify_admin_auth),
) -> dict:
    invocation = get_tool_invocation(tool_invocation_id=tool_invocation_id)
    if invocation is None:
        raise HTTPException(status_code=404, detail="tool invocation not found")
    return {
        "tool_invocation": invocation,
        "provider_runs": list_search_provider_runs(
            tool_invocation_id=tool_invocation_id,
            limit=50,
        ),
    }


@app.post("/debug/web-search/{account_id}/chat")
def debug_chat_web_search(
    account_id: str,
    payload: WebSearchDebugChatRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    text = str(payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    turn_payload = OpenClawTurnRequest(
        message_id=f"debug-web-search-chat-{uuid.uuid4().hex[:12]}",
        channel="debug-web-search",
        channel_account_id=account_id,
        account_id=account_id,
        sender_id="web-search-debug",
        sender_name="Web Search Debug",
        chat_id=account_id,
        chat_type="private",
        session_key=account_id,
        message_type="text",
        text=text,
        raw={
            "source": "web_search_debug",
            "web_search_provider_override": str(payload.provider or "").strip() or None,
        },
    )
    with override_provider_order(_debug_provider_override(payload.provider)):
        turn = handle_openclaw_turn(
            turn_payload,
            background_loop=_background_loop,
            force_web_search_enabled=bool(payload.force_web_search_enabled),
        )
    return {
        "status": "ok",
        "provider_override": str(payload.provider or "").strip() or None,
        "turn": turn.model_dump(),
        "conversation": _web_search_debug_conversation(account_id),
        "tool_invocations": list_tool_invocations(
            account_id=account_id,
            tool_name="web_search",
            limit=50,
        ),
        "provider_runs": list_search_provider_runs(
            account_id=account_id,
            limit=50,
        ),
    }


@app.post("/debug/web-search/{account_id}/run")
def debug_run_web_search(
    account_id: str,
    payload: WebSearchDebugRunRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    query = str(payload.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")

    provider_override = _debug_provider_override(payload.provider)
    provider_override_name = str(payload.provider or "").strip() or None

    session_bundle = get_or_create_session(
        account_id=account_id,
        channel="debug",
        sender_id="web-search-debug",
        sender_name="Web Search Debug",
        chat_id=account_id,
        session_key=f"debug-web-search-{account_id}",
    )
    session = session_bundle["session"]
    tool_call_id = f"call_debug_web_search_run_{uuid.uuid4().hex[:12]}"
    args = {
        "query": query,
        "count": payload.count,
        "language": payload.language,
        "country": payload.country,
        "freshness": payload.freshness,
        "date_after": payload.date_after,
        "date_before": payload.date_before,
    }
    invocation = create_tool_invocation(
        account_id=account_id,
        session_id=int(session["id"]),
        message_id=f"debug-web-search-run-{uuid.uuid4().hex[:12]}",
        tool_call_id=tool_call_id,
        tool_name="web_search",
        args={**args, "provider_override": provider_override_name},
        status="running",
    )
    ctx = SimpleNamespace(
        account_id=account_id,
        session=session,
        message_id=f"debug-web-search-run-{uuid.uuid4().hex[:12]}",
    )
    started = datetime.now()
    with override_provider_order(provider_override):
        result = handle_web_search(
            args,
            ctx,
            tool_call_id=tool_call_id,
            tool_invocation_id=int(invocation["id"]),
        )
    latency_ms = int((datetime.now() - started).total_seconds() * 1000)
    status_value = "failed" if result.get("status") == "failed" or result.get("error") else "succeeded"
    updated_invocation = update_tool_invocation(
        tool_invocation_id=int(invocation["id"]),
        status=status_value,
        result=result,
        latency_ms=latency_ms,
        error=result.get("error") if status_value == "failed" else None,
        finished=True,
    )
    return {
        "status": "ok",
        "account_id": account_id,
        "provider_override": provider_override_name,
        "result": result,
        "tool_invocation": updated_invocation,
        "provider_runs": list_search_provider_runs(
            tool_invocation_id=int(invocation["id"]),
            limit=50,
        ),
    }


@app.post("/debug/web-search/{account_id}/simulate")
def debug_simulate_web_search(
    account_id: str,
    payload: WebSearchDebugSimulationRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    query = str(payload.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    status_value = str(payload.status or "succeeded").strip().lower()
    if status_value not in {"running", "queued", "succeeded", "failed"}:
        raise HTTPException(
            status_code=400,
            detail="status must be one of: running, queued, succeeded, failed",
        )

    session_bundle = get_or_create_session(
        account_id=account_id,
        channel="debug",
        sender_id="web-search-debug",
        sender_name="Web Search Debug",
        chat_id=account_id,
        session_key=f"debug-web-search-{account_id}",
    )
    session_id = int(session_bundle["session"]["id"])
    provider = (
        str(payload.provider or "").strip()
        or getattr(settings, "web_search_default_provider", "duckduckgo")
    )
    args = {"query": query, "count": payload.count}
    finished = status_value in {"queued", "succeeded", "failed"}
    if status_value == "queued":
        result = {
            "status": "queued",
            "task_id": f"debug-task-{uuid.uuid4().hex[:12]}",
            "query": query,
        }
    elif status_value == "failed":
        result = {
            "status": "failed",
            "query": query,
            "error": payload.error or "debug simulated provider failure",
        }
    elif status_value == "running":
        result = {"status": "running", "query": query}
    else:
        result = {
            "status": "succeeded",
            "query": query,
            "provider": provider,
            "results": _fake_web_search_results(query=query, count=payload.count),
        }

    invocation = create_tool_invocation(
        account_id=account_id,
        session_id=session_id,
        message_id=f"debug-web-search-{uuid.uuid4().hex[:12]}",
        tool_call_id=f"call_debug_web_search_{uuid.uuid4().hex[:12]}",
        tool_name="web_search",
        args=args,
        status=status_value,
        result=result,
        latency_ms=payload.latency_ms,
        error=payload.error if status_value == "failed" else None,
        finished=finished,
    )

    provider_run = None
    if status_value != "queued":
        provider_status = "failed" if status_value == "failed" else status_value
        provider_run = create_search_provider_run(
            account_id=account_id,
            tool_invocation_id=int(invocation["id"]),
            provider=provider,
            attempt=1,
            status=provider_status,
            request=args,
            response=result if status_value == "succeeded" else {},
            latency_ms=payload.latency_ms,
            error=payload.error if status_value == "failed" else None,
            finished=status_value in {"succeeded", "failed"},
        )

    return {
        "status": "ok",
        "account_id": account_id,
        "tool_invocation": invocation,
        "provider_run": provider_run,
    }


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
    verification = create_phone_verification(phone=phone, code=code, expires_minutes=settings.otp_expires_minutes)
    try:
        send_otp(phone=phone, code=code)
    except Exception:
        invalidate_verifications_for_phone(phone)
        logger.exception("sms: send failed for phone=%s", phone)
        raise HTTPException(status_code=500, detail="短信发送失败，请稍后重试")

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


def _register_platform_user_with_otp(
    *,
    phone: str,
    display_name: Optional[str],
    otp_token: str,
) -> dict:
    try:
        normalized_phone = normalize_phone(phone)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    verification = consume_valid_verification_token(
        verified_token=otp_token,
        phone=normalized_phone,
    )
    if verification is None:
        raise HTTPException(status_code=400, detail="注册凭证无效或已过期")

    try:
        return create_or_get_platform_user_by_phone(
            phone=normalized_phone,
            display_name=display_name,
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))


@app.post("/web/register")
def web_register(payload: WebRegisterRequest) -> dict:
    platform_user = _register_platform_user_with_otp(
        phone=payload.phone,
        display_name=payload.display_name,
        otp_token=payload.otp_token,
    )
    return {
        "status": "ok",
        "platform_user": platform_user,
        "subscription": get_latest_subscription_for_user(
            platform_user_id=platform_user["id"],
        ),
    }


@app.post("/web/register-and-binding-intent")
def web_register_and_binding_intent(
    payload: WebRegisterAndBindingIntentRequest,
) -> dict:
    platform_user = _register_platform_user_with_otp(
        phone=payload.phone,
        display_name=payload.display_name,
        otp_token=payload.otp_token,
    )
    try:
        account_result = get_or_create_default_ai4all_account_for_user(
            platform_user_id=platform_user["id"],
            display_name=None,
            plan="free",
        )
        wallet = get_wallet_summary(
            account_id=account_result["account"]["id"],
            ensure_grant=True,
        )
        binding_intent = create_binding_intent(
            platform_user_id=platform_user["id"],
            account_id=account_result["account"]["id"],
            channel=payload.channel or "openclaw-weixin",
        )
        binding_intent = _start_openclaw_qr_for_binding(binding_intent)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    session = create_platform_user_session(platform_user_id=platform_user["id"], days=7)
    return {
        "status": "ok",
        "session_token": session["token"],
        "platform_user": platform_user,
        "account": account_result["account"],
        "profile": account_result["profile"],
        "owner_binding": account_result["owner_binding"],
        "subscription": account_result["subscription"],
        "wallet": wallet,
        "binding_intent": binding_intent,
        "binding_mode": "openclaw_gateway_qr",
        "next_step": "scan_qr_and_wait_for_completion",
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


def _require_session(authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization.removeprefix("Bearer ").strip()
    platform_user = get_platform_user_by_session_token(token=token)
    if platform_user is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新验证")
    return platform_user


@app.post("/web/binding-intents")
def web_create_binding_intent(
    payload: WebCreateBindingIntentRequest,
    platform_user=Depends(_require_session),
) -> dict:
    account_result = get_or_create_default_ai4all_account_for_user(
        platform_user_id=platform_user["id"],
        display_name=None,
        plan="free",
    )
    try:
        binding_intent = create_binding_intent(
            platform_user_id=platform_user["id"],
            account_id=account_result["account"]["id"],
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


class WebLoginRequest(BaseModel):
    verified_token: str
    phone: str


@app.post("/web/login")
def web_login(payload: WebLoginRequest) -> dict:
    """Exchange a verified OTP token for a session token.

    Handles both new and returning users in one call:
    - Creates or finds platform_user and default account.
    - Creates a 7-day session token.
    - If no active WeChat binding exists, also starts a new binding intent (QR).
    - Returns has_active_binding so the frontend can decide to show QR or go to dashboard.
    """
    try:
        normalized_phone = normalize_phone(payload.phone)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    verification = consume_valid_verification_token(
        verified_token=payload.verified_token,
        phone=normalized_phone,
    )
    if verification is None:
        raise HTTPException(status_code=400, detail="验证凭证无效或已过期")

    platform_user = create_or_get_platform_user_by_phone(
        phone=normalized_phone,
        display_name=None,
    )
    account_result = get_or_create_default_ai4all_account_for_user(
        platform_user_id=platform_user["id"],
        display_name=None,
        plan="free",
    )
    wallet = get_wallet_summary(
        account_id=account_result["account"]["id"],
        ensure_grant=True,
    )
    session = create_platform_user_session(platform_user_id=platform_user["id"], days=7)
    bindings = list_channel_bindings_for_account(account_id=account_result["account"]["id"])
    has_active_binding = len(bindings) > 0

    # If no binding yet, proactively create a binding intent so the frontend can show QR immediately
    binding_intent = None
    if not has_active_binding:
        try:
            binding_intent = create_binding_intent(
                platform_user_id=platform_user["id"],
                account_id=account_result["account"]["id"],
                channel="openclaw-weixin",
            )
            binding_intent = _start_openclaw_qr_for_binding(binding_intent)
        except Exception as err:
            logger.warning("web_login: failed to create binding intent: %s", err)

    return {
        "status": "ok",
        "session_token": session["token"],
        "platform_user": platform_user,
        "account": account_result["account"],
        "subscription": account_result["subscription"],
        "wallet": wallet,
        "has_active_binding": has_active_binding,
        "binding_intent": binding_intent,
        "binding_mode": "openclaw_gateway_qr",
    }


@app.get("/web/me")
def web_me(platform_user=Depends(_require_session)) -> dict:
    account_result = get_or_create_default_ai4all_account_for_user(
        platform_user_id=platform_user["id"],
        display_name=None,
        plan="free",
    )
    wallet = get_wallet_summary(
        account_id=account_result["account"]["id"],
        ensure_grant=True,
    )
    return {
        "status": "ok",
        "platform_user": platform_user,
        "account": account_result["account"],
        "subscription": account_result["subscription"],
        "wallet": wallet,
    }


@app.get("/web/me/wallet")
def web_me_wallet(platform_user=Depends(_require_session)) -> dict:
    account_result = get_or_create_default_ai4all_account_for_user(
        platform_user_id=platform_user["id"],
        display_name=None,
        plan="free",
    )
    wallet = get_wallet_summary(
        account_id=account_result["account"]["id"],
        ensure_grant=True,
    )
    if wallet is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    return {
        "status": "ok",
        "account_id": account_result["account"]["id"],
        "wallet": wallet,
        "ledger": list_wallet_ledger(account_id=account_result["account"]["id"], limit=20),
    }


@app.get("/web/me/bindings")
def web_me_bindings(platform_user=Depends(_require_session)) -> dict:
    account_result = get_or_create_default_ai4all_account_for_user(
        platform_user_id=platform_user["id"],
        display_name=None,
        plan="free",
    )
    bindings = list_channel_bindings_for_account(account_id=account_result["account"]["id"])
    return {
        "status": "ok",
        "account_id": account_result["account"]["id"],
        "bindings": bindings,
    }


@app.post("/web/me/unbind")
def web_me_unbind(
    payload: WebUnbindRequest,
    platform_user=Depends(_require_session),
) -> dict:
    account_result = get_or_create_default_ai4all_account_for_user(
        platform_user_id=platform_user["id"],
        display_name=None,
        plan="free",
    )
    account_id = account_result["account"]["id"]

    bindings_before_unbind = list_channel_bindings_for_account(account_id=account_id)
    openclaw_cleanup = _cleanup_openclaw_weixin_accounts(bindings_before_unbind)
    stats = unbind_account_channel(account_id=account_id)

    if not payload.keep_memories:
        wipe_stats = wipe_account_data(account_id=account_id)
        stats.update(wipe_stats)
        profile_dir = account_profile_dir(account_id)
        if profile_dir.exists():
            shutil.rmtree(profile_dir)

    return {
        "status": "ok",
        "keep_memories": payload.keep_memories,
        "account_id": account_id,
        "openclaw_cleanup": openclaw_cleanup,
        "stats": stats,
    }


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
    owner_bindings = list_account_owner_bindings_for_account(account_id=account_id)
    active_owner = next((binding for binding in owner_bindings if binding.get("status") == "active"), None)
    platform_user = (
        get_platform_user(platform_user_id=str(active_owner["platform_user_id"]))
        if active_owner else None
    )
    binding_intents = [
        _binding_intent_for_view(intent, account_id=account_id)
        for intent in list_binding_intents_for_account(account_id=account_id)
    ]
    recent_traces = list_debug_traces(account_id=account_id, limit=10)
    return {
        "account": account,
        "platform_user": _platform_user_for_view(platform_user, account_id=account_id),
        "owner_bindings": owner_bindings,
        "binding_intents": binding_intents,
        "channel_bindings": list_channel_bindings_for_account(account_id=account_id),
        "profile": _profile_for_view(
            get_profile_for_account(account_id=account_id) or {},
            account_id=account_id,
        ),
        "sessions": list_sessions_for_account(account_id=account_id),
        "recent_traces": [_trace_for_view(trace) for trace in recent_traces],
        **(
            _debug_redaction_payload(account_id=account_id)
            if _can_bypass_redaction_for_account(account_id)
            else {"redacted": True}
        ),
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
    content = path.read_text(encoding="utf-8")
    if _can_bypass_redaction_for_account(account_id):
        return {
            "account_id": account_id,
            "path": str(path),
            "content": content,
            "agent_context": context.metadata(),
            **_debug_redaction_payload(account_id=account_id),
        }
    return {
        "account_id": account_id,
        "path": str(path),
        "content_redacted": True,
        "content_chars": len(content),
        "agent_context": context.metadata(),
        "redacted": True,
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


@app.get("/admin/accounts/{account_id}/wallet")
def admin_account_wallet(
    account_id: str,
    limit: int = 20,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    wallet = get_wallet_summary(
        account_id=account_id,
        ensure_grant=False,
        create_if_missing=False,
    )
    return {
        "account_id": account_id,
        "wallet": wallet,
        "ledger": list_wallet_ledger(account_id=account_id, limit=limit) if wallet else [],
        "redacted": True,
    }


@app.get("/admin/accounts/{account_id}/proactive-overview")
def admin_account_proactive_overview(
    account_id: str,
    limit: int = 20,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
    return {
        "account_id": account_id,
        "proactive_state": _proactive_state_for_overview(
            get_proactive_account_state(account_id=account_id)
        ),
        "reminders": [
            _redact_text_field(reminder)
            for reminder in list_reminders_for_account(account_id=account_id, limit=limit)
        ],
        "commitments": [
            _redact_text_field(_redact_text_field(commitment), field="reason")
            for commitment in list_proactive_commitments_for_account(
                account_id=account_id,
                limit=limit,
            )
        ],
        "content_invitations": [
            _content_invitation_for_overview(invitation)
            for invitation in list_content_invitations_for_account(
                account_id=account_id,
                limit=limit,
            )
        ],
        "outbound_messages": [
            _redact_text_field(message)
            for message in list_outbound_messages(account_id=account_id, limit=limit)
        ],
        "redacted": True,
    }


@app.get("/admin/accounts/{account_id}/proactive-state")
def admin_get_proactive_account_state(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {
        "account_id": account_id,
        "proactive_state": get_proactive_account_state(account_id=account_id),
    }


@app.patch("/admin/accounts/{account_id}/proactive-state")
def admin_update_proactive_account_state(
    account_id: str,
    payload: ProactiveAccountStateUpdateRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")

    updates = payload.model_dump(exclude_unset=True)
    if "next_scan_at" in updates:
        updates["next_scan_at"] = _normalize_optional_state_datetime(
            updates["next_scan_at"],
            field_name="next_scan_at",
        )
    if "cooldown_until" in updates:
        updates["cooldown_until"] = _normalize_optional_state_datetime(
            updates["cooldown_until"],
            field_name="cooldown_until",
        )
    state = upsert_proactive_account_state(
        account_id=account_id,
        **updates,
    )
    return {
        "status": "ok",
        "account_id": account_id,
        "proactive_state": state,
    }


@app.post("/admin/accounts/{account_id}/proactive-check-candidate-draft")
def admin_generate_account_check_candidate_draft(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    result = generate_account_check_candidate_draft(account_id=account_id)
    return {"status": "ok", "result": result}


@app.post("/admin/accounts/{account_id}/proactive-check-candidate-draft/promote")
def admin_promote_account_check_candidate_draft(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    result = promote_account_check_candidate_draft(account_id=account_id)
    return {"status": "ok", "result": result}


@app.delete("/admin/accounts/{account_id}/proactive-check-candidate-draft")
def admin_clear_account_check_candidate_draft(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    result = clear_account_check_candidate_draft(account_id=account_id)
    return {"status": "ok", "result": result}


@app.post("/admin/accounts/{account_id}/proactive-check/run-once")
def admin_run_account_proactive_check_once(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    current = datetime.now()
    decision = decide_account_check_action(account_id=account_id, now=current)
    execution = execute_account_check_decision(decision=decision, now=current)
    if execution.get("status") == "sent":
        content_generation = {
            "action": "no_op",
            "account_id": account_id,
            "reason": "companion_followup_sent_this_run",
            "evaluated_at": current.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
            "metadata": {},
        }
    else:
        content_generation = generate_content_invitation_candidate(
            account_id=account_id,
            now=current,
        )

    invitation = content_generation.get("content_invitation")
    content_generation_metadata = content_generation.get("metadata") or {}
    display = {
        "content_invitation_generated": bool(invitation),
        "reason": None if invitation else content_generation.get("reason"),
        "detail": None if invitation else content_generation_metadata.get("reply"),
        "content_invitation": invitation,
    }
    return {
        "status": "ok",
        "account_id": account_id,
        "account_check": {
            "decision": decision,
            "execution": execution,
        },
        "content_invitation_generation": content_generation,
        "display": display,
    }


@app.get("/admin/accounts/{account_id}/commitments")
def admin_list_account_commitments(
    account_id: str,
    status: Optional[str] = None,
    limit: int = 50,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    return {
        "account_id": account_id,
        "commitments": list_proactive_commitments_for_account(
            account_id=account_id,
            status=status,
            limit=limit,
        ),
    }


@app.post("/admin/commitments/{commitment_id}/cancel")
def admin_cancel_commitment(
    commitment_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    commitment = get_proactive_commitment(commitment_id=commitment_id)
    if commitment is None:
        raise HTTPException(status_code=404, detail="commitment not found")
    cancelled = cancel_proactive_commitment(
        commitment_id=commitment_id,
        error="admin_cancelled",
    )
    return {"status": "ok", "commitment": cancelled}


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
        source_type="manual_admin",
        actor_type="admin",
        actor_id="admin_api",
    )


@app.get("/admin/accounts/{account_id}/dreaming")
def admin_list_account_dreaming(
    account_id: str,
    limit: int = 50,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    runs = list_dreaming_runs(account_id=account_id, limit=limit)
    items = list_dreaming_memory_items(account_id=account_id, limit=limit)
    return {
        "account_id": account_id,
        "runs": [summarize_dreaming_run_for_debug(run) for run in runs],
        "items": [summarize_memory_item_for_debug(item) for item in items],
        "redacted": True,
    }


@app.get("/admin/dreaming/runs/{run_id}")
def admin_get_dreaming_run(
    run_id: int,
    _: None = Depends(verify_admin_auth),
) -> dict:
    run = get_dreaming_run(run_id=run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="dreaming run not found")
    items = list_dreaming_memory_items(dreaming_run_id=run_id, limit=200)
    return {
        "run": summarize_dreaming_run_for_debug(run),
        "items": [summarize_memory_item_for_debug(item) for item in items],
        "redacted": True,
    }


@app.get("/admin/dreaming/items")
def admin_list_dreaming_items(
    account_id: Optional[str] = None,
    apply_status: Optional[str] = None,
    limit: int = 100,
    _: None = Depends(verify_admin_auth),
) -> dict:
    return {
        "items": [
            summarize_memory_item_for_debug(item)
            for item in list_dreaming_memory_items(
                account_id=account_id,
                apply_status=apply_status,
                limit=limit,
            )
        ],
        "redacted": True,
    }


@app.get("/admin/dreaming/items/{item_id}/events")
def admin_list_memory_item_events(
    item_id: int,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_dreaming_memory_item(item_id=item_id) is None:
        raise HTTPException(status_code=404, detail="memory item not found")
    events = list_memory_events(memory_item_id=item_id, limit=50)
    return {
        "item_id": item_id,
        "events": [
            {
                "id": event["id"],
                "account_id": event["account_id"],
                "memory_item_id": event.get("memory_item_id"),
                "event_type": event["event_type"],
                "actor_type": event["actor_type"],
                "actor_id": event.get("actor_id"),
                "diff_chars": len(event.get("diff_text") or ""),
                "created_at": event.get("created_at"),
            }
            for event in events
        ],
        "redacted": True,
    }


@app.post("/admin/dreaming/items/{item_id}/rollback")
def admin_rollback_memory_item(
    item_id: int,
    _: None = Depends(verify_admin_auth),
) -> dict:
    result = rollback_memory_item(
        item_id=item_id,
        actor_type="admin",
        actor_id="admin_api",
    )
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail="memory item not found")
    if result.get("item"):
        result["item"] = summarize_memory_item_for_debug(result["item"])
        result["redacted"] = True
    return result


# ---------------------------------------------------------------------------
# Admin — proactive scheduler
# ---------------------------------------------------------------------------

@app.get("/admin/proactive/scheduler")
def admin_proactive_scheduler_status(_: None = Depends(verify_admin_auth)) -> dict:
    scheduler = get_proactive_scheduler()
    return {
        "enabled": bool(getattr(settings, "proactive_scheduler_enabled", False)),
        "configured": {
            "interval_seconds": settings.proactive_scheduler_interval_seconds,
            "batch_size": settings.proactive_scheduler_batch_size,
            "bypass_quiet_hours": settings.proactive_scheduler_bypass_quiet_hours,
            "account_check_interval_seconds": settings.proactive_account_check_interval_seconds,
        },
        "scheduler": scheduler.status() if scheduler else None,
    }


@app.post("/admin/proactive/scheduler/run-once")
async def admin_proactive_scheduler_run_once(
    limit: int = 20,
    bypass_quiet_hours: bool = False,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
    result = await run_proactive_scheduler_once(
        batch_size=limit,
        bypass_quiet_hours=bypass_quiet_hours,
        account_check_interval_seconds=settings.proactive_account_check_interval_seconds,
    )
    dreaming = await asyncio.to_thread(run_daily_dreaming_scan, limit=limit)
    result["daily_dreaming"] = dreaming
    return {"status": "ok", "run": result}


@app.get("/admin/dreaming/scheduler")
def admin_dreaming_scheduler_status(_: None = Depends(verify_admin_auth)) -> dict:
    scheduler = get_dreaming_scheduler()
    return {
        "enabled": bool(getattr(settings, "dreaming_scheduler_enabled", False)),
        "configured": {
            "interval_seconds": settings.dreaming_scheduler_interval_seconds,
            "batch_size": settings.dreaming_scheduler_batch_size,
            "business_day_start_hour": settings.conversation_session_business_day_start_hour,
        },
        "scheduler": scheduler.status() if scheduler else None,
    }


@app.post("/admin/dreaming/scheduler/run-once")
async def admin_dreaming_scheduler_run_once(
    limit: int = 100,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    result = await run_dreaming_scheduler_once(batch_size=limit)
    return {"status": "ok", "run": result}


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
    account_id = session.get("account_id")
    redacted = _redacted_flag_for_account(account_id)
    return {
        "session": _session_for_view(session),
        "profile": _profile_for_view(
            get_profile_for_session(session_id=session_id) or {},
            account_id=account_id,
        ),
        "messages": [
            _message_for_view(message, account_id=account_id)
            for message in list_session_messages(session_id=session_id, limit=100)
        ],
        **(_debug_redaction_payload(account_id=account_id) if not redacted else {"redacted": True}),
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
    messages = list_recent_message_raw(limit=limit)
    return {
        "messages": [
            _message_for_view(message)
            for message in messages
        ],
        "redacted": not any(_can_bypass_redaction_for_account(message.get("account_id")) for message in messages),
    }


@app.get("/admin/messages/{message_db_id}/raw")
def admin_message_raw(
    message_db_id: int,
    _: None = Depends(verify_admin_auth),
) -> dict:
    message = get_message_raw(message_db_id=message_db_id)
    if message is None:
        raise HTTPException(status_code=404, detail="message not found")
    account_id = message.get("account_id")
    redacted = _redacted_flag_for_account(account_id)
    return {
        "message": _message_for_view(message),
        **(_debug_redaction_payload(account_id=account_id) if not redacted else {"redacted": True}),
    }


@app.get("/admin/debug/traces")
def admin_debug_traces(
    account_id: Optional[str] = None,
    session_id: Optional[int] = None,
    limit: int = 50,
    _: None = Depends(verify_admin_auth),
) -> dict:
    return {
        "traces": [
            _trace_for_view(t) for t in list_debug_traces(
                account_id=account_id,
                session_id=session_id,
                limit=limit,
            )
        ]
    }


@app.get("/admin/debug/traces/{trace_id}")
def admin_debug_trace(
    trace_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    trace = get_debug_trace(trace_id=trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    account_id = trace.get("account_id")
    redacted = _redacted_flag_for_account(account_id)
    return {
        "trace": _trace_for_view(trace),
        **(_debug_redaction_payload(account_id=account_id) if not redacted else {"redacted": True}),
    }


@app.get("/admin/plaintext/messages/{message_db_id}/raw")
def admin_plaintext_message_raw(
    message_db_id: int,
    reason: Optional[str] = None,
    admin_user: dict = Depends(get_admin_user),
) -> dict:
    message = get_message_raw(message_db_id=message_db_id)
    if message is None:
        raise HTTPException(status_code=404, detail="message not found")
    grant = _require_plaintext_access(
        admin_user=admin_user,
        account_id=str(message.get("account_id")),
        resource_type="message",
    )
    _audit_plaintext_access(
        admin_user=admin_user,
        action="view_message_raw",
        resource_type="message",
        resource_id=str(message_db_id),
        account_id=message.get("account_id"),
        request_path=f"/admin/plaintext/messages/{message_db_id}/raw",
        reason=reason,
        grant_id=int(grant["id"]) if grant else None,
    )
    return {"message": message, "plaintext": True}


@app.get("/admin/plaintext/debug-traces/{trace_id}")
def admin_plaintext_debug_trace(
    trace_id: str,
    reason: Optional[str] = None,
    admin_user: dict = Depends(get_admin_user),
) -> dict:
    trace = get_debug_trace(trace_id=trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    grant = _require_plaintext_access(
        admin_user=admin_user,
        account_id=str(trace.get("account_id")),
        resource_type="debug_trace",
    )
    _audit_plaintext_access(
        admin_user=admin_user,
        action="view_debug_trace",
        resource_type="debug_trace",
        resource_id=trace_id,
        account_id=trace.get("account_id"),
        request_path=f"/admin/plaintext/debug-traces/{trace_id}",
        reason=reason,
        grant_id=int(grant["id"]) if grant else None,
    )
    return {"trace": trace, "plaintext": True}


@app.get("/admin/plaintext/accounts/{account_id}/user-profile")
def admin_plaintext_user_profile(
    account_id: str,
    reason: Optional[str] = None,
    admin_user: dict = Depends(get_admin_user),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    grant = _require_plaintext_access(
        admin_user=admin_user,
        account_id=account_id,
        resource_type="user_profile",
    )
    path = ensure_user_profile(account_id)
    context = read_agent_context(account_id)
    _audit_plaintext_access(
        admin_user=admin_user,
        action="view_user_profile",
        resource_type="user_profile",
        resource_id=account_id,
        account_id=account_id,
        request_path=f"/admin/plaintext/accounts/{account_id}/user-profile",
        reason=reason,
        grant_id=int(grant["id"]) if grant else None,
    )
    return {
        "account_id": account_id,
        "path": str(path),
        "content": path.read_text(encoding="utf-8"),
        "agent_context": context.metadata(),
        "plaintext": True,
    }


@app.get("/admin/access-events")
def admin_access_events(
    account_id: Optional[str] = None,
    plaintext: Optional[bool] = None,
    limit: int = 100,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    return {
        "events": list_admin_access_events(
            account_id=account_id,
            plaintext=plaintext,
            limit=limit,
        )
    }


@app.get("/admin/users")
def admin_users(
    limit: int = 100,
    _: dict = Depends(require_admin_user),
) -> dict:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    return {"admin_users": list_admin_users(limit=limit)}


@app.post("/admin/plaintext-grants")
def admin_create_plaintext_grant(
    payload: PlaintextGrantRequest,
    admin_user: dict = Depends(get_admin_user),
) -> dict:
    reason = str(payload.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")
    account_scope = _clean_scope_list(payload.account_scope, field_name="account_scope")
    resource_scope = _clean_scope_list(payload.resource_scope, field_name="resource_scope")
    grant = create_admin_plaintext_grant(
        requester_admin_user_id=str(admin_user["id"]),
        reason=reason,
        account_scope=account_scope,
        resource_scope=resource_scope,
        time_scope_start=_normalize_optional_state_datetime(
            payload.time_scope_start,
            field_name="time_scope_start",
        ),
        time_scope_end=_normalize_optional_state_datetime(
            payload.time_scope_end,
            field_name="time_scope_end",
        ),
    )
    insert_admin_access_event(
        admin_user_id=admin_user["id"],
        action="request_plaintext_grant",
        resource_type="plaintext_grant",
        resource_id=str(grant["id"]),
        account_id=account_scope[0] if len(account_scope) == 1 else None,
        plaintext=False,
        reason=reason,
        request_path="/admin/plaintext-grants",
        metadata={"account_scope": account_scope, "resource_scope": resource_scope},
    )
    return {"status": "ok", "grant": grant}


@app.get("/admin/plaintext-grants")
def admin_list_plaintext_grants(
    requester_admin_user_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    admin_user: dict = Depends(get_admin_user),
) -> dict:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    requester = requester_admin_user_id
    if admin_user.get("role") != "admin":
        requester = str(admin_user["id"])
    return {
        "grants": list_admin_plaintext_grants(
            requester_admin_user_id=requester,
            status=status,
            limit=limit,
        )
    }


@app.post("/admin/plaintext-grants/{grant_id}/approve")
def admin_approve_plaintext_grant(
    grant_id: int,
    admin_user: dict = Depends(require_admin_user),
) -> dict:
    grant = get_admin_plaintext_grant(grant_id=grant_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="plaintext grant not found")
    if grant["status"] != "pending":
        raise HTTPException(status_code=400, detail="only pending grants can be approved")
    now = datetime.now()
    approved_at = now.strftime("%Y-%m-%d %H:%M:%S")
    expires_at = (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    updated = update_admin_plaintext_grant_status(
        grant_id=grant_id,
        status="approved",
        approver_admin_user_id=str(admin_user["id"]),
        approved_at=approved_at,
        expires_at=expires_at,
    )
    insert_admin_access_event(
        admin_user_id=admin_user["id"],
        action="approve_plaintext_grant",
        resource_type="plaintext_grant",
        resource_id=str(grant_id),
        account_id=updated["account_scope"][0] if updated and len(updated.get("account_scope") or []) == 1 else None,
        plaintext=False,
        request_path=f"/admin/plaintext-grants/{grant_id}/approve",
    )
    return {"status": "ok", "grant": updated}


@app.post("/admin/plaintext-grants/{grant_id}/reject")
def admin_reject_plaintext_grant(
    grant_id: int,
    admin_user: dict = Depends(require_admin_user),
) -> dict:
    grant = get_admin_plaintext_grant(grant_id=grant_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="plaintext grant not found")
    if grant["status"] != "pending":
        raise HTTPException(status_code=400, detail="only pending grants can be rejected")
    updated = update_admin_plaintext_grant_status(
        grant_id=grant_id,
        status="rejected",
        approver_admin_user_id=str(admin_user["id"]),
    )
    insert_admin_access_event(
        admin_user_id=admin_user["id"],
        action="reject_plaintext_grant",
        resource_type="plaintext_grant",
        resource_id=str(grant_id),
        plaintext=False,
        request_path=f"/admin/plaintext-grants/{grant_id}/reject",
    )
    return {"status": "ok", "grant": updated}


@app.post("/admin/plaintext-grants/{grant_id}/revoke")
def admin_revoke_plaintext_grant(
    grant_id: int,
    admin_user: dict = Depends(require_admin_user),
) -> dict:
    grant = get_admin_plaintext_grant(grant_id=grant_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="plaintext grant not found")
    if grant["status"] not in {"pending", "approved"}:
        raise HTTPException(status_code=400, detail="only pending or approved grants can be revoked")
    updated = update_admin_plaintext_grant_status(
        grant_id=grant_id,
        status="revoked",
        approver_admin_user_id=str(admin_user["id"]),
        revoked_at=_now_db_time(),
    )
    insert_admin_access_event(
        admin_user_id=admin_user["id"],
        action="revoke_plaintext_grant",
        resource_type="plaintext_grant",
        resource_id=str(grant_id),
        plaintext=False,
        request_path=f"/admin/plaintext-grants/{grant_id}/revoke",
    )
    return {"status": "ok", "grant": updated}


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
        raw_identity=identity_response_metadata(identity, account_id),
    )
    session = session_state["session"]
    trace_id = payload.trace_id or f"trace-{uuid.uuid4()}"
    metadata = dict(payload.metadata or {})
    metadata["identity"] = identity_response_metadata(identity, account_id)
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
        "metadata": identity_response_metadata(identity, account_id),
    }


@app.post("/openclaw/turn", response_model=OpenClawTurnResponse)
def openclaw_turn(
    payload: OpenClawTurnRequest,
    _: None = Depends(verify_bridge_auth),
) -> OpenClawTurnResponse:
    return handle_openclaw_turn(payload, background_loop=_background_loop)
