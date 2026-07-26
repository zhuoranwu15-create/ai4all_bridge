"""Versioned mobile App API: OTP auth, account bootstrap, chat, and batch ASR."""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
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
from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    get_duplicate_reply_record,
    get_or_create_default_ai4all_account_for_user,
    get_platform_user,
    get_session_for_account_and_key,
    list_session_messages_before,
    revoke_platform_user_session,
    update_platform_user_profile,
)
from app.bootstrap.product_registry import ZHAOXI_APP_ID
from app.products.zhaoxi.api.contracts import (
    AccountDeletionResponse,
    AppConfigResponse,
    MeResponse,
    ProfileOptionsResponse,
    ProfileUpdateResponse,
)
from app.products.zhaoxi.application.account_deletion import delete_account_now
from app.products.zhaoxi.domain.user_profile import (
    MAX_NICKNAME_CHARS,
    UserProfileError,
    optional_user_avatar_ref,
    profile_options_catalog,
    resolve_user_avatar_ref,
    validate_nickname,
)
from app.platform.moderation.text_sanitizer import (
    FIELD_USER_NICKNAME,
    TextRejected,
    TextSanitizerUnavailable,
    sanitize_text,
)
from app.platform.auth.identity import ResolvedIdentity
from app.routers.deps import _require_session
from app.routers.web import (
    SendOtpRequest,
    VerifyOtpRequest,
    _register_platform_user_with_otp_result,
    web_send_otp,
    web_verify_otp,
)
from app.time_utils import beijing_now
from app.agent_runtime.turns.service import ChannelTurnInput
from app.products.zhaoxi.application.turns import run_zhaoxi_turn as run_turn_for_account


router = APIRouter(tags=["app-v1"])

# App「朝夕相伴」(app_id=zhaoxi) 的默认 AI 名字。App 首建账号且用户未起名时用它兜底,
# 使 IDENTITY 播种为「你的名字是 朝夕」、LLM 自称与 UI 展示一致(见 §9.3 C4/L4)。
# get_or_create 不覆盖已有账号,故用户在其它渠道已起的名不受影响。
_ZHAOXI_DEFAULT_AI_NAME = "朝夕"

# App 端契约版本。客户端用它判断服务端是否已交付某一轮字段；改契约时必须同步上调。
CLIENT_CONTRACT_VERSION = "2026-07-26"

_CLIENT_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_BEIJING_TZ = timezone(timedelta(hours=8))


def _public_time(value: Optional[str]) -> Optional[str]:
    """把 DB 北京 naive 时间转成带 +08:00 的公开 ISO 时间（TIME-001）。

    与世界端点同口径：公开时间一律显式带时区，客户端不按设备时区猜。
    """
    if not value:
        return None
    return (
        datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        .replace(tzinfo=_BEIJING_TZ)
        .isoformat()
    )


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


class UpdateProfileRequest(BaseModel):
    """ME-01 Profile 更新入参；两字段均可选，但不能同时为空。"""

    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = Field(default=None, max_length=MAX_NICKNAME_CHARS)
    avatar_key: Optional[str] = Field(default=None, max_length=64)


class AccountDeletionRequest(BaseModel):
    """ME-06/07 注销入参；原因是受控取值，不接受自由文本。

    ``confirm`` 必须显式传 true：注销**立即且不可撤销**地删除聊天记录与记忆，空 body
    就能触发这种操作太危险——误调一次没有任何补救手段。
    """

    model_config = ConfigDict(extra="forbid")

    confirm: bool
    reason_code: Optional[str] = Field(default=None, max_length=32)


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


def _public_platform_user(platform_user: dict) -> dict:
    """真人公开 DTO。手机号只出脱敏值，任何情况下不返回明文或完整尾号以外的片段。"""
    phone = str(platform_user.get("phone") or "")
    avatar_key = platform_user.get("avatar_key")
    return {
        "id": platform_user["id"],
        "phone_masked": f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 7 else "***",
        "display_name": platform_user.get("display_name"),
        "avatar_key": avatar_key,
        "avatar_ref": optional_user_avatar_ref(avatar_key),
    }


def _public_deletion_request(record: dict) -> dict:
    """注销流水公开 DTO；不返回 purge_stats_json 等运营内部字段。"""
    return {
        "request_id": str(record["id"]),
        "status": str(record["status"]),
        "reason_code": record.get("reason_code"),
        "executed_at": _public_time(record.get("executed_at")),
    }


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


def _optional_account_for_user(principal: SessionPrincipal) -> Optional[dict]:
    """解析当前真人的 legacy account；没有则返回 None。

    P1 开启后新用户在确认居民之前**恒无** account，这是正常状态而不是错误——
    `/me` 必须能在这种状态下工作，否则 App 被杀进程后无法恢复未完成的世界引导。
    账号被禁用仍然是错误（403），语义不变。
    """
    result = get_active_bound_account_for_user_in_app(
        platform_user_id=principal.platform_user_id,
        app_id=principal.app_id,
    )
    if result is None:
        return None
    if result["account"].get("status") == "disabled":
        raise HTTPException(status_code=403, detail="account_disabled")
    return result


def _account_for_user(principal: SessionPrincipal) -> dict:
    """`/chat/*` 等确实需要 runtime account 的端点使用；无 account 时 409。"""
    result = _optional_account_for_user(principal)
    if result is None:
        raise HTTPException(status_code=409, detail="account_not_ready")
    return result


def _world_summary(platform_user_id: str) -> Optional[dict]:
    """返回 home world 的引导摘要；能力关闭或尚未建世界时为 None。

    刻意**只读不建**：建世界仍然只由 `POST /worlds/home/bootstrap` 负责，
    `/me` 不产生副作用。
    """
    if not bool(getattr(settings, "companion_world_p1_enabled", False)):
        return None
    from app.products.zhaoxi.application import SqlCompanionWorldRepository

    world = SqlCompanionWorldRepository().get_home_universe(platform_user_id)
    if world is None:
        return None
    return {
        "id": world.id,
        "status": world.status,
        "onboarding_state": world.onboarding_state,
    }


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


# 公开 capability → 内部 flag 的唯一映射。刻意不暴露 flag 名、阈值和 scheduler 开关，
# 只回答「这个能力现在能不能用」。两处口径必须一起改，否则客户端会按错误的能力做分支。
#
# 注意两条不是一一映射的：
#   * human_chat_send —— 真人聊天的**读**由 p1 门控（随 resident_world），只有**发送**由
#     human_chat flag 门控。给一个笼统的 human_chat 会让客户端在开读关写时整块隐藏历史。
#   * resident_lifecycle —— 映射 commit（真正会让居民下线的开关），不是只跑评估不落地的
#     evaluation。
def _companion_world_capabilities() -> dict:
    world_enabled = bool(getattr(settings, "companion_world_p1_enabled", False))

    def _gated(flag: str) -> bool:
        return world_enabled and bool(getattr(settings, flag, False))

    return {
        "resident_world": world_enabled,
        "world_feed": _gated("companion_world_feed_enabled"),
        "app_notifications": _gated("companion_world_app_inbox_enabled"),
        "resident_lifecycle": _gated("companion_world_lifecycle_commit_enabled"),
        "mailbox": _gated("companion_world_mailbox_enabled"),
        "world_visits": _gated("companion_world_visits_enabled"),
        "human_chat_send": _gated("companion_world_human_chat_enabled"),
    }


@router.get("/app/config", response_model=AppConfigResponse)
def app_config(response: Response) -> dict:
    scene_id = str(settings.aliyun_captcha_scene_id or "").strip()
    prefix = str(settings.aliyun_captcha_prefix or "").strip()
    _no_store(response)
    return {
        "captcha": {
            "provider": "aliyun",
            "scene_id": scene_id,
            "prefix": prefix,
            "configured": bool(scene_id and prefix),
        },
        # 字段只加不改：旧客户端继续只读 voice_input。
        "features": {
            "voice_input": is_asr_available(),
            **_companion_world_capabilities(),
        },
        "limits": {
            "message_chars": 4000,
            "audio_bytes": int(settings.asr_max_audio_bytes),
            "audio_duration_ms": int(settings.asr_max_duration_ms),
        },
        "client_contract_version": CLIENT_CONTRACT_VERSION,
        "server_time": beijing_now().isoformat(timespec="seconds"),
        "minimum_supported_version": "0.1.0",
        # 分平台最低版本先按「不拦」上线，值等实际灰度需求再调（CONFIG-201）。
        "minimum_supported_version_by_platform": {"ios": "0.0.0", "android": "0.0.0"},
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


@router.get("/me", response_model=MeResponse)
def app_me(
    response: Response,
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    account_result = _optional_account_for_user(principal)
    platform_user = get_platform_user(platform_user_id=principal.platform_user_id)
    if platform_user is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新验证")
    _no_store(response)
    return {
        "status": "ok",
        "platform_user": _public_platform_user(platform_user),
        "account": _public_account(account_result) if account_result else None,
        "world": _world_summary(principal.platform_user_id),
        "server_time": beijing_now().isoformat(timespec="seconds"),
    }


@router.get("/me/profile-options", response_model=ProfileOptionsResponse)
def app_profile_options(
    response: Response,
    _principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    """下发昵称限额与受控头像表（ME-01）；客户端不硬编码头像枚举。"""
    _no_store(response)
    return {"status": "ok", **profile_options_catalog()}


@router.patch("/me/profile", response_model=ProfileUpdateResponse)
def app_update_profile(
    payload: UpdateProfileRequest,
    response: Response,
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    """更新真人昵称/头像（ME-01）。

    两个字段都可单独提交；``None`` 表示本次不改。昵称除字符白名单外还要过 D-B 清洗器
    —— 它会展示给来访的真实好友，风险面与自建角色名一致；清洗器不可用时 fail closed。
    """
    if payload.display_name is None and payload.avatar_key is None:
        raise HTTPException(status_code=422, detail="profile_update_empty")
    display_name: Optional[str] = None
    avatar_key: Optional[str] = None
    try:
        if payload.display_name is not None:
            display_name = validate_nickname(payload.display_name)
        if payload.avatar_key is not None:
            # 只为校验 key 合法性；ref 由响应侧统一重新解析。
            resolve_user_avatar_ref(payload.avatar_key)
            avatar_key = payload.avatar_key.strip()
    except UserProfileError as err:
        raise HTTPException(status_code=422, detail=err.code) from err
    if display_name is not None:
        try:
            display_name = sanitize_text(
                text=display_name,
                field_kind=FIELD_USER_NICKNAME,
                max_chars=MAX_NICKNAME_CHARS,
            ).text
        except TextRejected as err:
            raise HTTPException(status_code=422, detail="content_rejected") from err
        except TextSanitizerUnavailable as err:
            raise HTTPException(
                status_code=503, detail="content_review_unavailable"
            ) from err
        # 清洗器可能改写出空串或超长；再过一次结构校验，避免写进不合法的展示名。
        try:
            display_name = validate_nickname(display_name)
        except UserProfileError as err:
            raise HTTPException(status_code=422, detail=err.code) from err
    updated = update_platform_user_profile(
        platform_user_id=principal.platform_user_id,
        display_name=display_name,
        avatar_key=avatar_key,
    )
    if updated is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新验证")
    _no_store(response)
    return {"status": "ok", "platform_user": _public_platform_user(updated)}


@router.post("/me/account/deletion", response_model=AccountDeletionResponse)
def app_delete_account(
    payload: AccountDeletionRequest,
    response: Response,
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    """注销账号（ME-06/07）：**立即**删除聊天记录与相关记忆，不可撤销。

    清除范围与保留项见 :mod:`app.products.zhaoxi.application.account_deletion`。返回后
    该真人的全部登录态已失效，客户端应直接回到登录页——继续用旧 token 会拿到 401。

    没有查询/撤销接口：不存在待执行状态，查了也永远是「已完成」。
    """
    if not payload.confirm:
        raise HTTPException(status_code=422, detail="deletion_not_confirmed")
    try:
        result = delete_account_now(
            platform_user_id=principal.platform_user_id,
            app_id=principal.app_id,
            reason_code=payload.reason_code,
            now=beijing_now().replace(tzinfo=None, microsecond=0),
        )
    except ValueError as err:
        raise HTTPException(status_code=422, detail="reason_code_invalid") from err
    _no_store(response)
    return {"status": "ok", "request": _public_deletion_request(result["record"])}


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
                "created_at": _public_time(item["created_at"]),
            }
            for item in messages
        ],
        "next_cursor": messages[0]["id"] if len(messages) == limit else None,
    }


def _duplicate_turn_response(reply_row: dict) -> dict:
    """legacy `/chat/turn` 的幂等重放响应，与世界端 turn 同口径回放原 message_id（TURN-001）。"""
    return {
        "status": "ok",
        "reply": reply_row.get("content"),
        "no_reply": False,
        "metadata": {
            "deduplicated": True,
            "message_id": reply_row.get("message_id"),
        },
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

    duplicate = get_duplicate_reply_record(
        account_id=account_id,
        reply_to_message_id=mapped_message_id,
    )
    if duplicate is not None:
        _no_store(response)
        return _duplicate_turn_response(duplicate)

    lock = _turn_lock(account_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="turn_in_progress")
    try:
        duplicate = get_duplicate_reply_record(
            account_id=account_id,
            reply_to_message_id=mapped_message_id,
        )
        if duplicate is not None:
            _no_store(response)
            return _duplicate_turn_response(duplicate)

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
