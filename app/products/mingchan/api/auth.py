"""鸣蝉 Native 身份 API；只签发和接受 mingchan audience session。"""
from __future__ import annotations

from typing import NoReturn, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response

from app.bootstrap.product_registry import (
    MINGCHAN_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
)
from app.config import settings
from app.db import (
    SessionPrincipal,
    get_platform_user,
    revoke_platform_user_session,
)
from app.platform.auth.captcha import verify_captcha
from app.platform.auth.phone_otp import (
    PhoneOtpError,
    send_phone_otp,
    verify_phone_otp,
)
from app.platform.auth.sms import generate_otp, send_otp
from app.products.mingchan.api.contracts import (
    MingchanLogoutResponse,
    MingchanMeResponse,
    MingchanOtpSendRequest,
    MingchanOtpVerifyRequest,
    MingchanOtpVerifyResponse,
    MingchanSessionRequest,
    MingchanSessionResponse,
    MingchanStatusResponse,
)
from app.products.mingchan.api.deps import (
    build_mingchan_enabled_dependency,
    build_mingchan_session_dependency,
)
from app.products.mingchan.application.identity import create_mingchan_login_session
from app.products.mingchan.infrastructure.persistence.companion_world import get_universe
from app.time_utils import beijing_now


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _public_platform_user(platform_user: dict) -> dict:
    phone = str(platform_user["phone"])
    return {
        "id": str(platform_user["id"]),
        "phone_masked": f"{phone[:3]}****{phone[-4:]}",
        "display_name": platform_user.get("display_name"),
        "avatar_key": platform_user.get("avatar_key"),
    }


def _bearer_token(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    return token


def _raise_phone_otp_http_error(error: PhoneOtpError) -> NoReturn:
    raise HTTPException(status_code=error.status_code, detail=error.detail) from None


def build_router(
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
    *,
    config=None,
) -> APIRouter:
    """创建鸣蝉身份 router；测试可注入已启用注册表与隔离配置。"""

    registry.require_registered(MINGCHAN_APP_ID)
    otp_config = settings if config is None else config

    require_enabled_product = build_mingchan_enabled_dependency(registry)
    require_session = build_mingchan_session_dependency(registry)
    router = APIRouter(
        tags=["mingchan-identity"],
        dependencies=[Depends(require_enabled_product)],
    )

    @router.post("/auth/otp/send", response_model=MingchanStatusResponse)
    def send_otp_code(payload: MingchanOtpSendRequest, response: Response) -> dict:
        try:
            result = send_phone_otp(
                phone=payload.phone,
                captcha_verify_param=payload.captcha_verify_param,
                max_per_phone_per_hour=otp_config.aliyun_sms_max_per_phone_per_hour,
                expires_minutes=otp_config.otp_expires_minutes,
                verify_captcha_fn=verify_captcha,
                generate_code_fn=generate_otp,
                send_code_fn=send_otp,
            )
        except PhoneOtpError as err:
            _raise_phone_otp_http_error(err)
        _no_store(response)
        return result

    @router.post("/auth/otp/verify", response_model=MingchanOtpVerifyResponse)
    def verify_otp_code(
        payload: MingchanOtpVerifyRequest,
        response: Response,
    ) -> dict:
        try:
            result = verify_phone_otp(
                phone=payload.phone,
                code=payload.code,
                token_expires_minutes=otp_config.otp_token_expires_minutes,
            )
        except PhoneOtpError as err:
            _raise_phone_otp_http_error(err)
        _no_store(response)
        return result

    @router.post("/auth/session", response_model=MingchanSessionResponse)
    def create_session(payload: MingchanSessionRequest, response: Response) -> dict:
        try:
            result = create_mingchan_login_session(
                phone=payload.phone,
                verified_token=payload.verified_token,
                invite_code=payload.invite_code,
                registry=registry,
            )
        except ValueError as err:
            detail = str(err)
            if detail == "invalid_otp_token":
                detail = "验证凭证无效或已过期"
            raise HTTPException(status_code=400, detail=detail) from None

        platform_user = result["registration"]["platform_user"]
        session = result["session"]
        _no_store(response)
        return {
            "status": "ok",
            "access_token": session["token"],
            "expires_at": session["expires_at"],
            "is_new_user": bool(result["registration"]["is_new_membership"]),
            "platform_user": _public_platform_user(platform_user),
        }

    @router.get("/me", response_model=MingchanMeResponse)
    def get_me(
        response: Response,
        principal: SessionPrincipal = Depends(require_session),
    ) -> dict:
        platform_user = get_platform_user(platform_user_id=principal.platform_user_id)
        if platform_user is None:
            raise HTTPException(status_code=401, detail="登录已过期，请重新验证")
        _no_store(response)
        return {
            "status": "ok",
            "platform_user": _public_platform_user(platform_user),
            "world": (
                {
                    "id": world["id"],
                    "status": world["status"],
                    "onboarding_state": world["onboarding_state"],
                }
                if (
                    world := get_universe(
                        owner_platform_user_id=principal.platform_user_id,
                        expected_app_id=MINGCHAN_APP_ID,
                    )
                )
                else None
            ),
            "server_time": beijing_now().isoformat(timespec="seconds"),
        }

    @router.delete("/auth/session/current", response_model=MingchanLogoutResponse)
    def logout(
        response: Response,
        authorization: Optional[str] = Header(default=None),
        _principal: SessionPrincipal = Depends(require_session),
    ) -> dict:
        revoke_platform_user_session(token=_bearer_token(authorization))
        _no_store(response)
        return {"status": "ok"}

    return router


__all__ = ["build_router"]
