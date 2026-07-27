"""微信小程序 jscode2session + getuserphonenumber；纯 httpx，不碰 DB、不碰 FastAPI。

`app/products/nooki/api/auth.py` 是唯一调用方；这里只负责跟微信官方接口打交道，
失败统一抛 `WeChatAuthError`，由 API 层翻译成 HTTP 状态码。

阶段3 改造：不再用 session_key 解密 encryptedData（旧路径需要保存 session_key 这个
敏感凭证），改用微信官方 getuserphonenumber API 直接用 phone_code 换手机号明文。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict

import httpx

from app.config import settings

_CODE2SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"
_ACCESS_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
_GET_PHONE_URL = "https://api.weixin.qq.com/wxa/business/getuserphonenumber"
_TIMEOUT_SECONDS = 10.0


class WeChatAuthError(Exception):
    """微信登录相关的不可恢复错误（未配置、code2session 失败、换手机号失败等）。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(detail or code)


# ---------------------------------------------------------------------------
# access_token 进程级缓存（微信 access_token 2h 有效，避免每次登录都重新获取）
# ---------------------------------------------------------------------------
_access_token_lock = threading.Lock()
_access_token_cache: Dict[str, Dict[str, Any]] = {}


def _get_access_token() -> str:
    """获取（带缓存的）小程序全局 access_token。"""

    appid = settings.nooki_wx_appid
    now = time.time()
    with _access_token_lock:
        cached = _access_token_cache.get(appid)
        if cached and cached["expires_at"] > now + 60:
            return cached["token"]
    try:
        response = httpx.get(
            _ACCESS_TOKEN_URL,
            params={
                "grant_type": "client_credential",
                "appid": appid,
                "secret": settings.nooki_wx_app_secret,
            },
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as err:
        raise WeChatAuthError("wx_access_token_failed", str(err)) from err

    errcode = payload.get("errcode")
    if errcode:
        raise WeChatAuthError(
            "wx_access_token_failed", f"errcode={errcode} errmsg={payload.get('errmsg')}"
        )
    token = payload.get("access_token")
    if not token:
        raise WeChatAuthError("wx_access_token_failed", "微信返回缺少 access_token")
    expires_in = int(payload.get("expires_in") or 7200)
    with _access_token_lock:
        _access_token_cache[appid] = {"token": token, "expires_at": now + expires_in}
    return token


def code2session(login_code: str) -> Dict[str, Any]:
    """用小程序 wx.login() 的 code 换 openid/unionid，不向上暴露 session_key。"""

    if not settings.nooki_wx_appid or not settings.nooki_wx_app_secret:
        raise WeChatAuthError("wx_not_configured", "NOOKI_WX_APPID/NOOKI_WX_APP_SECRET 未配置")
    try:
        response = httpx.get(
            _CODE2SESSION_URL,
            params={
                "appid": settings.nooki_wx_appid,
                "secret": settings.nooki_wx_app_secret,
                "js_code": login_code,
                "grant_type": "authorization_code",
            },
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as err:
        raise WeChatAuthError("wx_code2session_failed", str(err)) from err

    errcode = payload.get("errcode")
    if errcode:
        raise WeChatAuthError(
            "wx_code2session_failed", f"errcode={errcode} errmsg={payload.get('errmsg')}"
        )
    openid = payload.get("openid")
    if not openid:
        raise WeChatAuthError("wx_code2session_failed", "微信返回缺少 openid")
    return {"openid": openid, "unionid": payload.get("unionid")}


def get_phone_number(phone_code: str) -> Dict[str, Any]:
    """用 wx.getPhoneNumber 回调的 code 换手机号明文（微信 getuserphonenumber API）。

    不再需要 session_key 解密 encryptedData——phone_code 是微信一次性凭证，用完即弃，
    服务端不保存任何敏感凭证。
    """

    if not settings.nooki_wx_appid or not settings.nooki_wx_app_secret:
        raise WeChatAuthError("wx_not_configured", "NOOKI_WX_APPID/NOOKI_WX_APP_SECRET 未配置")
    access_token = _get_access_token()
    try:
        response = httpx.post(
            _GET_PHONE_URL,
            params={"access_token": access_token},
            json={"code": phone_code},
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as err:
        raise WeChatAuthError("wx_get_phone_failed", str(err)) from err

    errcode = payload.get("errcode")
    if errcode:
        raise WeChatAuthError(
            "wx_get_phone_failed", f"errcode={errcode} errmsg={payload.get('errmsg')}"
        )
    phone_info = payload.get("phone_info") or {}
    phone_number = phone_info.get("purePhoneNumber") or phone_info.get("phoneNumber")
    if not phone_number:
        raise WeChatAuthError("wx_get_phone_failed", "微信返回缺少手机号字段")
    return {"phone_number": str(phone_number), "country_code": phone_info.get("countryCode")}


__all__ = ["WeChatAuthError", "code2session", "get_phone_number"]
