"""鸣蝉 Native 客户端配置与真人资料 API。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile

from app.bootstrap.product_registry import (
    MINGCHAN_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
)
from app.config import settings
from app.db import SessionPrincipal, update_platform_user_profile
from app.platform.media.access import signing_configured
from app.platform.media.asr import (
    ASRNotConfiguredError,
    ASRProviderError,
    is_asr_available,
    transcribe_audio,
)
from app.platform.moderation.text_sanitizer import (
    FIELD_USER_NICKNAME,
    TextRejected,
    TextSanitizerUnavailable,
    sanitize_text,
)
from app.products.mingchan.api.contracts import (
    MingchanAppConfigResponse,
    MingchanAccountDeletionRequest,
    MingchanAccountDeletionResponse,
    MingchanProfileOptionsResponse,
    MingchanProfileUpdateRequest,
    MingchanProfileUpdateResponse,
)
from app.products.mingchan.api.deps import (
    build_mingchan_enabled_dependency,
    build_mingchan_session_dependency,
)
from app.products.mingchan.domain.user_profile import (
    MAX_NICKNAME_CHARS,
    MingchanUserProfileError,
    optional_user_avatar_ref,
    profile_options_catalog,
    resolve_user_avatar_ref,
    validate_nickname,
)
from app.products.mingchan.application.account_deletion import delete_account_now
from app.time_utils import BEIJING_TZ, beijing_now

CLIENT_CONTRACT_VERSION = "2026-08-04"
MAX_WISH_TEXT_CHARS = 500
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


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _public_platform_user(platform_user: dict) -> dict:
    """返回不含明文手机号的鸣蝉真人 DTO。"""

    phone = str(platform_user.get("phone") or "")
    avatar_key = platform_user.get("avatar_key")
    return {
        "id": str(platform_user["id"]),
        "phone_masked": (
            f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 7 else "***"
        ),
        "display_name": platform_user.get("display_name"),
        "avatar_key": avatar_key,
        "avatar_ref": optional_user_avatar_ref(avatar_key),
    }


def _public_db_time(value: Optional[str]) -> Optional[str]:
    """把数据库北京 naive 时间转换成带时区的客户端时间。"""

    if not value:
        return None
    parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    return parsed.replace(tzinfo=BEIJING_TZ).isoformat()


def _capabilities(config) -> dict:
    world_enabled = bool(getattr(config, "mingchan_p1_enabled", False))
    media_ready = signing_configured()

    def gated(flag: str) -> bool:
        return world_enabled and bool(getattr(config, flag, False))

    def media_gated(flag: str) -> bool:
        return gated(flag) and media_ready

    return {
        "resident_world": world_enabled,
        "world_feed": gated("mingchan_feed_enabled"),
        "app_notifications": gated("mingchan_app_inbox_enabled"),
        "resident_lifecycle": gated("mingchan_lifecycle_commit_enabled"),
        "mailbox": gated("mingchan_mailbox_enabled"),
        "world_visits": gated("mingchan_visits_enabled"),
        "human_chat_send": gated("mingchan_human_chat_enabled"),
        "chat_image_message": media_gated(
            "mingchan_chat_image_enabled"
        ),
        "chat_voice_message": media_gated(
            "mingchan_chat_voice_enabled"
        ),
        "feed_image_post": media_gated("mingchan_feed_image_enabled"),
        "resident_wish_create": gated("mingchan_mailbox_enabled"),
    }


def build_router(
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
    *,
    config=None,
) -> APIRouter:
    """创建固定鸣蝉 audience 的 App 配置与资料 router。"""

    app_config = settings if config is None else config
    require_enabled = build_mingchan_enabled_dependency(registry)
    require_session = build_mingchan_session_dependency(registry)
    router = APIRouter(
        tags=["mingchan-app"],
        dependencies=[Depends(require_enabled)],
    )

    @router.get("/app/config", response_model=MingchanAppConfigResponse)
    def get_app_config(response: Response) -> dict:
        product = registry.require_enabled(MINGCHAN_APP_ID)
        scene_id = str(getattr(app_config, "aliyun_captcha_scene_id", "") or "")
        prefix = str(getattr(app_config, "aliyun_captcha_prefix", "") or "")
        _no_store(response)
        return {
            "product": {
                "app_id": product.app_id,
                "default_language": product.default_language,
            },
            "captcha": {
                "provider": "aliyun",
                "scene_id": scene_id,
                "prefix": prefix,
                "configured": bool(scene_id and prefix),
            },
            "features": {
                "voice_input": is_asr_available(),
                **_capabilities(app_config),
            },
            "limits": {
                "message_chars": 4000,
                "audio_bytes": int(app_config.asr_max_audio_bytes),
                "audio_duration_ms": int(app_config.asr_max_duration_ms),
                "image_bytes_max": int(app_config.media_image_max_bytes),
                "image_count_max": int(app_config.media_image_count_max),
                "voice_bytes_max": int(app_config.media_voice_max_bytes),
                "voice_duration_ms_max": int(
                    app_config.media_voice_max_duration_ms
                ),
                "wish_text_chars": MAX_WISH_TEXT_CHARS,
                "wish_daily_max": int(app_config.mingchan_wish_daily_max),
            },
            "client_contract_version": CLIENT_CONTRACT_VERSION,
            "server_time": beijing_now().isoformat(timespec="seconds"),
            "minimum_supported_version": "0.1.0",
            "minimum_supported_version_by_platform": {
                "ios": "0.0.0",
                "android": "0.0.0",
            },
        }

    @router.get(
        "/me/profile-options",
        response_model=MingchanProfileOptionsResponse,
    )
    def get_profile_options(
        response: Response,
        _principal: SessionPrincipal = Depends(require_session),
    ) -> dict:
        """下发鸣蝉真人昵称限制和受控头像目录。"""

        _no_store(response)
        return {"status": "ok", **profile_options_catalog()}

    @router.patch("/me/profile", response_model=MingchanProfileUpdateResponse)
    def update_profile(
        payload: MingchanProfileUpdateRequest,
        response: Response,
        principal: SessionPrincipal = Depends(require_session),
    ) -> dict:
        """校验并更新共享真人的鸣蝉展示资料。"""

        if payload.display_name is None and payload.avatar_key is None:
            raise HTTPException(status_code=422, detail="profile_update_empty")
        display_name: Optional[str] = None
        avatar_key: Optional[str] = None
        try:
            if payload.display_name is not None:
                display_name = validate_nickname(payload.display_name)
            if payload.avatar_key is not None:
                resolve_user_avatar_ref(payload.avatar_key)
                avatar_key = payload.avatar_key.strip()
        except MingchanUserProfileError as err:
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
                    status_code=503,
                    detail="content_review_unavailable",
                ) from err
            try:
                display_name = validate_nickname(display_name)
            except MingchanUserProfileError as err:
                raise HTTPException(status_code=422, detail=err.code) from err
        updated = update_platform_user_profile(
            platform_user_id=principal.platform_user_id,
            display_name=display_name,
            avatar_key=avatar_key,
        )
        if updated is None:
            raise HTTPException(status_code=401, detail="登录已过期，请重新验证")
        _no_store(response)
        return {
            "status": "ok",
            "platform_user": _public_platform_user(updated),
        }

    @router.post(
        "/me/account/deletion",
        response_model=MingchanAccountDeletionResponse,
    )
    def delete_account(
        payload: MingchanAccountDeletionRequest,
        response: Response,
        principal: SessionPrincipal = Depends(require_session),
    ) -> dict:
        """立即清除当前真人的鸣蝉 membership、session 与鸣蝉产品资产。"""

        if not payload.confirm:
            raise HTTPException(status_code=422, detail="deletion_not_confirmed")
        try:
            result = delete_account_now(
                platform_user_id=principal.platform_user_id,
                reason_code=payload.reason_code,
                now=beijing_now().replace(tzinfo=None, microsecond=0),
                registry=registry,
            )
        except ValueError as err:
            raise HTTPException(status_code=422, detail="reason_code_invalid") from err
        record = result["record"]
        _no_store(response)
        return {
            "status": "ok",
            "request": {
                "request_id": record["id"],
                "status": record["status"],
                "reason_code": record.get("reason_code"),
                "executed_at": _public_db_time(record.get("executed_at")),
            },
        }

    @router.post("/audio/transcriptions")
    async def transcribe_audio_input(
        response: Response,
        audio: UploadFile = File(...),
        duration_ms: int = Form(..., ge=1),
        language: str = Form(default="zh"),
        _principal: SessionPrincipal = Depends(require_session),
    ) -> dict:
        """校验并转写一段短语音；原始音频只在本次请求内存中使用。"""

        if duration_ms > int(app_config.asr_max_duration_ms):
            raise HTTPException(status_code=413, detail="audio_too_long")
        content_type = str(audio.content_type or "application/octet-stream").lower()
        if content_type not in _ALLOWED_AUDIO_TYPES:
            raise HTTPException(status_code=415, detail="unsupported_audio_type")
        max_bytes = int(app_config.asr_max_audio_bytes)
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

    return router


__all__ = ["CLIENT_CONTRACT_VERSION", "build_router"]
