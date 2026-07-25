"""Versioned mobile App API: OTP auth, account bootstrap, chat, and batch ASR."""

from __future__ import annotations

import re
import threading
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from pydantic import BaseModel, Field, field_validator

from app.platform.media.asr import (
    ASRNotConfiguredError,
    ASRProviderError,
    is_asr_available,
    transcribe_audio,
)
from app.platform.channels import CHANNEL_APP, CHANNELS
from app.config import settings
from app.db import (
    SessionPrincipal,
    create_platform_user_session,
    get_active_bound_account_for_user_in_app,
    get_duplicate_reply,
    get_or_create_default_ai4all_account_for_user,
    get_platform_user,
    get_session_for_account_and_key,
    list_session_messages_before,
    revoke_platform_user_session,
)
from app.bootstrap.product_registry import ZHAOXI_APP_ID
from app.platform.auth.identity import ResolvedIdentity
from app.routers.deps import _require_session
from app.routers.web import (
    SendOtpRequest,
    VerifyOtpRequest,
    _register_platform_user_with_otp_result,
    web_send_otp,
    web_verify_otp,
)
from app.agent_runtime.turns.service import ChannelTurnInput
from app.products.zhaoxi.application.turns import run_zhaoxi_turn as run_turn_for_account


router = APIRouter(tags=["app-v1"])

# App「朝夕相伴」(app_id=zhaoxi) 的默认 AI 名字。App 首建账号且用户未起名时用它兜底,
# 使 IDENTITY 播种为「你的名字是 朝夕」、LLM 自称与 UI 展示一致(见 §9.3 C4/L4)。
# get_or_create 不覆盖已有账号,故用户在其它渠道已起的名不受影响。
_ZHAOXI_DEFAULT_AI_NAME = "朝夕"

_CLIENT_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_ALLOWED_AUDIO_TYPES = {
    "audio/aac",
    "audio/m4a",
    "audio/mp4",
    "audio/mpeg",
    "audio/wav",
    "audio/webm",
    "audio/x-m4a",
    "application/octet-stream",
}
_turn_locks_guard = threading.Lock()
_turn_locks: dict[str, threading.Lock] = {}


class AppSessionRequest(BaseModel):
    phone: str
    verified_token: str = Field(min_length=8, max_length=256)
    invite_code: Optional[str] = Field(default=None, max_length=64)
    campaign_code: Optional[str] = Field(default=None, max_length=64)


class AppTurnRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    client_message_id: str = Field(min_length=8, max_length=64)

    @field_validator("text")
    @classmethod
    def clean_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("text must not be blank")
        return cleaned

    @field_validator("client_message_id")
    @classmethod
    def validate_message_id(cls, value: str) -> str:
        if not _CLIENT_MESSAGE_ID_RE.fullmatch(value):
            raise ValueError("invalid client_message_id")
        return value


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _public_account(result: dict) -> dict:
    account = result["account"]
    profile = result.get("profile") or {}
    return {
        "id": account["id"],
        "status": account.get("status"),
        "ai_display_name": (
            profile.get("display_name") or account.get("display_name") or _ZHAOXI_DEFAULT_AI_NAME
        ),
        "ai_subtitle": "陪你聊聊，也陪你慢慢认识自己",
    }


def _account_for_user(principal: SessionPrincipal) -> dict:
    result = get_active_bound_account_for_user_in_app(
        platform_user_id=principal.platform_user_id,
        app_id=principal.app_id,
    )
    if result is None:
        raise HTTPException(status_code=409, detail="account_not_ready")
    if result["account"].get("status") == "disabled":
        raise HTTPException(status_code=403, detail="account_disabled")
    return result


def _turn_lock(account_id: str) -> threading.Lock:
    with _turn_locks_guard:
        lock = _turn_locks.get(account_id)
        if lock is None:
            lock = threading.Lock()
            _turn_locks[account_id] = lock
        return lock


def _bearer_token(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    return token


@router.get("/app/config")
def app_config() -> dict:
    scene_id = str(settings.aliyun_captcha_scene_id or "").strip()
    prefix = str(settings.aliyun_captcha_prefix or "").strip()
    return {
        "captcha": {
            "provider": "aliyun",
            "scene_id": scene_id,
            "prefix": prefix,
            "configured": bool(scene_id and prefix),
        },
        "features": {"voice_input": is_asr_available()},
        "limits": {
            "message_chars": 4000,
            "audio_bytes": int(settings.asr_max_audio_bytes),
            "audio_duration_ms": int(settings.asr_max_duration_ms),
        },
        "minimum_supported_version": "0.1.0",
    }


@router.post("/auth/otp/send")
def app_send_otp(payload: SendOtpRequest) -> dict:
    return web_send_otp(payload)


@router.post("/auth/otp/verify")
def app_verify_otp(payload: VerifyOtpRequest) -> dict:
    result = web_verify_otp(payload)
    return {"status": result["status"], "verified_token": result["verified_token"]}


@router.post("/auth/session")
def app_create_session(payload: AppSessionRequest, response: Response) -> dict:
    registration = _register_platform_user_with_otp_result(
        phone=payload.phone,
        display_name=None,
        otp_token=payload.verified_token,
        invite_code=payload.invite_code,
        invalid_otp_detail="验证凭证无效或已过期",
    )
    platform_user = registration["platform_user"]
    if bool(getattr(settings, "companion_world_p1_enabled", False)):
        # P1 新流程：只复用既有 legacy account；零 binding（含真正新用户）不预建默认
        # runtime/binding，客户端收到 account:null 后进入 world bootstrap。
        account_result = get_active_bound_account_for_user_in_app(
            platform_user_id=platform_user["id"],
            app_id=ZHAOXI_APP_ID,
        )
    else:
        # flag 关闭保持旧 auth 行为：建/复用默认账号、赠权与 campaign 归因均逐字不变。
        account_result = get_or_create_default_ai4all_account_for_user(
            platform_user_id=platform_user["id"],
            display_name=_ZHAOXI_DEFAULT_AI_NAME,
            plan="free",
            campaign_code=payload.campaign_code,
            initial_channel=CHANNEL_APP,
            binding_method="app_otp",
        )
    session = create_platform_user_session(
        platform_user_id=platform_user["id"], app_id=ZHAOXI_APP_ID, days=30
    )
    _no_store(response)
    return {
        "status": "ok",
        "access_token": session["token"],
        "expires_at": session["expires_at"],
        # 保留旧响应字段名，但语义按冻结设计切为当前产品首次 membership。
        "is_new_user": bool(registration["is_new_membership"]),
        "platform_user": {
            "id": platform_user["id"],
            "phone_masked": f"{platform_user['phone'][:3]}****{platform_user['phone'][-4:]}",
        },
        "account": _public_account(account_result) if account_result else None,
        "welcome_message": (
            f"你好，我是{_ZHAOXI_DEFAULT_AI_NAME}。想聊聊此刻的心情，还是随便说点什么？"
            if account_result
            else None
        ),
    }


@router.delete("/auth/session/current")
def app_logout(
    response: Response,
    authorization: Optional[str] = Header(default=None),
    _principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    token = _bearer_token(authorization)
    revoke_platform_user_session(token=token)
    _no_store(response)
    return {"status": "ok"}


@router.get("/me")
def app_me(
    response: Response,
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    account_result = _account_for_user(principal)
    platform_user = get_platform_user(platform_user_id=principal.platform_user_id)
    if platform_user is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新验证")
    _no_store(response)
    phone = str(platform_user.get("phone") or "")
    return {
        "status": "ok",
        "platform_user": {
            "id": platform_user["id"],
            "phone_masked": f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 7 else "***",
        },
        "account": _public_account(account_result),
    }


@router.get("/chat/messages")
def app_messages(
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    before_id: Optional[int] = Query(default=None, ge=1),
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    account_result = _account_for_user(principal)
    account_id = account_result["account"]["id"]
    active_key = CHANNELS[CHANNEL_APP].active_session_key
    session = get_session_for_account_and_key(
        account_id=account_id,
        session_key=active_key,
    )
    if session is None:
        messages: list[dict] = []
    else:
        messages = list_session_messages_before(
            account_id=account_id,
            session_id=int(session["id"]),
            before_id=before_id,
            limit=limit,
        )
    _no_store(response)
    return {
        "messages": [
            {
                "id": item["id"],
                "message_id": item.get("message_id"),
                "role": item["role"],
                "text": item["content"],
                "created_at": item["created_at"],
            }
            for item in messages
        ],
        "next_cursor": messages[0]["id"] if len(messages) == limit else None,
    }


@router.post("/chat/turn")
def app_turn(
    payload: AppTurnRequest,
    response: Response,
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    account_result = _account_for_user(principal)
    account_id = account_result["account"]["id"]
    platform_user = get_platform_user(platform_user_id=principal.platform_user_id) or {}
    mapped_message_id = f"app:{account_id}:{payload.client_message_id}"

    duplicate_reply = get_duplicate_reply(
        account_id=account_id,
        reply_to_message_id=mapped_message_id,
    )
    if duplicate_reply is not None:
        _no_store(response)
        return {
            "status": "ok",
            "reply": duplicate_reply,
            "no_reply": False,
            "metadata": {"deduplicated": True},
        }

    lock = _turn_lock(account_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="turn_in_progress")
    try:
        duplicate_reply = get_duplicate_reply(
            account_id=account_id,
            reply_to_message_id=mapped_message_id,
        )
        if duplicate_reply is not None:
            _no_store(response)
            return {
                "status": "ok",
                "reply": duplicate_reply,
                "no_reply": False,
                "metadata": {"deduplicated": True},
            }

        identity = ResolvedIdentity(
            ai4all_account_id=account_id,
            session_key=f"app:{principal.platform_user_id}",
            channel=CHANNEL_APP,
            channel_account_id=principal.platform_user_id,
            sender_id=principal.platform_user_id,
            chat_id=None,
        )
        result = run_turn_for_account(
            ChannelTurnInput(
                account_id=account_id,
                app_id=principal.app_id,
                cap=CHANNELS[CHANNEL_APP],
                identity=identity,
                message_id=mapped_message_id,
                event_id=None,
                message_type="text",
                text=payload.text,
                media=None,
                raw={"source": "app_v1"},
                sender_name=platform_user.get("display_name"),
            )
        )
    finally:
        lock.release()

    if result.status == "rate_limited":
        raise HTTPException(status_code=429, detail=result.reply or "rate_limited")
    if result.status == "disabled":
        raise HTTPException(status_code=403, detail="account_disabled")
    _no_store(response)
    metadata = {
        "deduplicated": result.status == "duplicate",
        "message_id": result.metadata.get("reply_message_id"),
    }
    return {
        "status": "ok" if result.status == "duplicate" else result.status,
        "reply": result.reply,
        "no_reply": result.no_reply,
        "metadata": metadata,
    }


@router.post("/audio/transcriptions")
async def app_transcription(
    response: Response,
    audio: UploadFile = File(...),
    duration_ms: int = Form(..., ge=1),
    language: str = Form(default="zh"),
    _principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    if duration_ms > int(settings.asr_max_duration_ms):
        raise HTTPException(status_code=413, detail="audio_too_long")
    content_type = str(audio.content_type or "application/octet-stream").lower()
    if content_type not in _ALLOWED_AUDIO_TYPES:
        raise HTTPException(status_code=415, detail="unsupported_audio_type")
    max_bytes = int(settings.asr_max_audio_bytes)
    content = await audio.read(max_bytes + 1)
    await audio.close()
    if not content:
        raise HTTPException(status_code=422, detail="empty_audio")
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail="audio_too_large")
    try:
        transcript = transcribe_audio(
            content=content,
            filename=audio.filename or "recording.m4a",
            content_type=content_type,
            language=language,
        )
    except ASRNotConfiguredError as err:
        raise HTTPException(status_code=503, detail="asr_not_configured") from err
    except ASRProviderError as err:
        raise HTTPException(status_code=502, detail="asr_provider_failed") from err
    _no_store(response)
    return {
        "transcript": transcript,
        "duration_ms": duration_ms,
        "language": language,
    }
