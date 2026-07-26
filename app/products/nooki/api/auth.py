"""Nooki 微信小程序登录：首次一键手机号绑定 + 后续静默续期，均不发短信。

- `/auth/bind`：首次进入小程序时调用。前端拿 `wx.login()` 的 `code` +
  `wx.getPhoneNumber` 回调的 `encryptedData`/`iv`；服务端用 code 换 openid/session_key，
  再用 session_key 解密出手机号，建号（或复用既有 `platform_user`）并绑定 openid。
- `/auth/session`：此后每次打开小程序调用。只传 `wx.login()` 的 `code`；服务端换 openid，
  按已绑定的 `(app_id, openid)` 直接签发新 session，不再需要手机号弹窗。找不到绑定时返回
  404，前端据此回退去走 `/auth/bind`。

两个端点都不需要现有 session（登录本身），因此不挂 `require_product_session`。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from app.bootstrap.product_registry import NOOKI_APP_ID
from app.db import create_platform_user_session, register_platform_user_with_referral
from app.products.nooki.infrastructure.repositories.wx_identity import (
    bind_wx_identity,
    find_platform_user_id_by_openid,
)
from app.products.nooki.infrastructure.wechat_client import (
    WeChatAuthError,
    code2session,
    decrypt_phone_number,
)

router = APIRouter(tags=["nooki-auth"])

_SESSION_DAYS = 30


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


class WxBindRequest(BaseModel):
    code: str = Field(min_length=1, max_length=512)
    encrypted_data: str = Field(min_length=1)
    iv: str = Field(min_length=1)


class WxSessionRequest(BaseModel):
    code: str = Field(min_length=1, max_length=512)


@router.post("/auth/bind")
def nooki_auth_bind(payload: WxBindRequest, response: Response) -> dict:
    try:
        wx_session = code2session(payload.code)
        phone_info = decrypt_phone_number(
            encrypted_data=payload.encrypted_data,
            iv=payload.iv,
            session_key=wx_session["session_key"],
        )
    except WeChatAuthError as err:
        raise HTTPException(status_code=400, detail=err.code) from err

    openid = wx_session["openid"]
    existing_platform_user_id = find_platform_user_id_by_openid(app_id=NOOKI_APP_ID, openid=openid)
    if existing_platform_user_id is not None:
        # 幂等重试：openid 已绑定过，直接复用既有真人，不重复建号/写绑定。
        platform_user_id = existing_platform_user_id
        is_new_user = False
    else:
        try:
            registration = register_platform_user_with_referral(
                phone=phone_info["phone_number"],
                invite_code=None,
                verified_token=None,
                app_id=NOOKI_APP_ID,
            )
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
        platform_user_id = registration["platform_user"]["id"]
        is_new_user = bool(registration["is_new_membership"])
        bind_wx_identity(
            platform_user_id=platform_user_id,
            app_id=NOOKI_APP_ID,
            openid=openid,
            unionid=wx_session.get("unionid"),
        )

    session = create_platform_user_session(
        platform_user_id=platform_user_id, app_id=NOOKI_APP_ID, days=_SESSION_DAYS
    )
    _no_store(response)
    return {
        "status": "ok",
        "access_token": session["token"],
        "expires_at": session["expires_at"],
        "is_new_user": is_new_user,
        "platform_user_id": platform_user_id,
    }


@router.post("/auth/session")
def nooki_auth_session(payload: WxSessionRequest, response: Response) -> dict:
    try:
        wx_session = code2session(payload.code)
    except WeChatAuthError as err:
        raise HTTPException(status_code=400, detail=err.code) from err

    platform_user_id = find_platform_user_id_by_openid(
        app_id=NOOKI_APP_ID, openid=wx_session["openid"]
    )
    if platform_user_id is None:
        raise HTTPException(status_code=404, detail="wx_identity_not_bound")

    session = create_platform_user_session(
        platform_user_id=platform_user_id, app_id=NOOKI_APP_ID, days=_SESSION_DAYS
    )
    _no_store(response)
    return {
        "status": "ok",
        "access_token": session["token"],
        "expires_at": session["expires_at"],
    }


__all__ = ["router"]
