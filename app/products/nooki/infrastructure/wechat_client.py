"""微信小程序 jscode2session + 手机号一键组件解密；纯 httpx/cryptography，不碰 DB、不碰 FastAPI。

`app/products/nooki/api/auth.py` 是唯一调用方；这里只负责跟微信官方接口/加密算法打交道，
失败统一抛 `WeChatAuthError`，由 API 层翻译成 HTTP 状态码。
"""
from __future__ import annotations

import base64
import json
from typing import Any, Dict

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.config import settings

_CODE2SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"
_TIMEOUT_SECONDS = 10.0


class WeChatAuthError(Exception):
    """微信登录相关的不可恢复错误（未配置、code2session 失败、解密失败等）。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(detail or code)


def code2session(js_code: str) -> Dict[str, Any]:
    """用小程序 `wx.login()` 拿到的 code 换 openid/session_key（微信官方 jscode2session）。"""

    if not settings.nooki_wx_appid or not settings.nooki_wx_app_secret:
        raise WeChatAuthError("wx_not_configured", "NOOKI_WX_APPID/NOOKI_WX_APP_SECRET 未配置")
    try:
        response = httpx.get(
            _CODE2SESSION_URL,
            params={
                "appid": settings.nooki_wx_appid,
                "secret": settings.nooki_wx_app_secret,
                "js_code": js_code,
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
    session_key = payload.get("session_key")
    if not openid or not session_key:
        raise WeChatAuthError("wx_code2session_failed", "微信返回缺少 openid/session_key")
    return {"openid": openid, "session_key": session_key, "unionid": payload.get("unionid")}


def decrypt_phone_number(*, encrypted_data: str, iv: str, session_key: str) -> Dict[str, Any]:
    """AES-128-CBC 解密微信"手机号快速验证组件"返回的 encryptedData，取出手机号明文。

    key=session_key、iv=iv 均为 base64；解密后按 PKCS7 去填充再 JSON 解析（微信固定格式）。
    """

    try:
        key = base64.b64decode(session_key)
        iv_bytes = base64.b64decode(iv)
        ciphertext = base64.b64decode(encrypted_data)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv_bytes)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        pad_len = padded[-1]
        if not (1 <= pad_len <= 16) or pad_len > len(padded):
            raise ValueError("invalid PKCS7 padding")
        parsed = json.loads(padded[:-pad_len])
    except (ValueError, TypeError) as err:
        raise WeChatAuthError("wx_decrypt_failed", str(err)) from err

    phone_number = parsed.get("purePhoneNumber") or parsed.get("phoneNumber")
    if not phone_number:
        raise WeChatAuthError("wx_decrypt_failed", "解密结果缺少手机号字段")
    return {"phone_number": str(phone_number), "country_code": parsed.get("countryCode")}


__all__ = ["WeChatAuthError", "code2session", "decrypt_phone_number"]
