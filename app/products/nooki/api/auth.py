"""Nooki 微信小程序登录：首次一键手机号绑定 + 后续静默续期，均不发短信。

- `/auth/bind`：首次进入小程序时调用。前端拿 `wx.login()` 的 `login_code` +
  `wx.getPhoneNumber` 回调的 `phone_code`；服务端用 login_code 换 openid，
  用 phone_code 换手机号明文，建号（或复用既有 `platform_user`）并绑定 openid。
- `/auth/session`：此后每次打开小程序调用。只传 `wx.login()` 的 `login_code`；服务端换
  openid，按已绑定的 `(wx_appid, openid)` 直接签发新 session，不再需要手机号弹窗。
  找不到绑定时返回 404，前端据此回退去走 `/auth/bind`。

两个端点都不需要现有 session（登录本身），因此不挂 `require_product_session`。

阶段3 改造：从 `code + encrypted_data + iv`（需要 session_key 解密）改为
`login_code + phone_code`（微信官方 getuserphonenumber API），服务端不保存 session_key。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from app.bootstrap.product_registry import NOOKI_APP_ID
from app.config import settings
from app.db import (
    create_platform_user_session,
    get_platform_user_by_phone,
    register_platform_user_with_referral,
)
from app.products.nooki.infrastructure.repositories.wx_identity import (
    bind_wx_identity,
    find_platform_user_id_by_openid,
)
from app.products.nooki.infrastructure.wechat_client import (
    WeChatAuthError,
    code2session,
    get_phone_number,
)

router = APIRouter(tags=["nooki-auth"])

_SESSION_DAYS = 30


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _wx_appid() -> str:
    """返回当前配置的 Nooki 小程序 wx_appid（身份绑定唯一键的一部分）。"""

    return settings.nooki_wx_appid


class WxBindRequest(BaseModel):
    login_code: str = Field(min_length=1, max_length=512)
    phone_code: str = Field(min_length=1, max_length=512)


class WxSessionRequest(BaseModel):
    login_code: str = Field(min_length=1, max_length=512)


@router.post("/auth/bind")
def nooki_auth_bind(payload: WxBindRequest, response: Response) -> dict:
    """首次绑定：login_code 换 openid + phone_code 换手机号，建号并绑定 openid。"""

    try:
        wx_session = code2session(payload.login_code)
        phone_info = get_phone_number(payload.phone_code)
    except WeChatAuthError as err:
        raise HTTPException(status_code=400, detail=err.code) from err

    openid = wx_session["openid"]
    wx_appid = _wx_appid()

    # 1. openid 已绑定 → 复用既有 platform_user（幂等重试）
    existing_platform_user_id = find_platform_user_id_by_openid(
        wx_appid=wx_appid, openid=openid
    )
    if existing_platform_user_id is not None:
        # 冲突检测：openid 绑定的真人 vs 手机号对应的真人若不一致 → 409，不得自动改绑
        phone_user = get_platform_user_by_phone(phone=phone_info["phone_number"])
        if phone_user is not None and phone_user["id"] != existing_platform_user_id:
            raise HTTPException(status_code=409, detail="wx_identity_conflict")
        platform_user_id = existing_platform_user_id
        is_new_user = False
    else:
        # 2. openid 未绑定 → 用手机号建号/复用，再绑定 openid
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
            wx_appid=wx_appid,
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
    """静默续期：login_code 换 openid，按已绑定身份直接签发新 session。"""

    try:
        wx_session = code2session(payload.login_code)
    except WeChatAuthError as err:
        raise HTTPException(status_code=400, detail=err.code) from err

    platform_user_id = find_platform_user_id_by_openid(
        wx_appid=_wx_appid(), openid=wx_session["openid"]
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
