"""产品无关的手机号 OTP 发送与校验编排。"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from app.db.accounts import (
    count_verifications_last_hour,
    create_phone_verification,
    get_latest_active_verification,
    increment_verify_attempts,
    invalidate_other_verifications_for_phone,
    invalidate_verification,
    normalize_phone,
    set_verification_verified,
)

logger = logging.getLogger("ai4all.platform.auth.phone_otp")

_otp_send_master_lock = threading.Lock()
_otp_send_locks: dict[str, threading.Lock] = {}


class PhoneOtpError(ValueError):
    """可由产品 API 映射为 HTTP 响应的 OTP 业务错误。"""

    def __init__(self, *, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = detail


def _otp_send_lock_for(phone: str) -> threading.Lock:
    """返回进程内手机号锁，串行化检查、发送和旧验证码失效。"""

    with _otp_send_master_lock:
        lock = _otp_send_locks.get(phone)
        if lock is None:
            lock = threading.Lock()
            _otp_send_locks[phone] = lock
        return lock


def send_phone_otp(
    *,
    phone: str,
    captcha_verify_param: str,
    max_per_phone_per_hour: int,
    expires_minutes: int,
    verify_captcha_fn: Callable[[str], bool],
    generate_code_fn: Callable[[], str],
    send_code_fn: Callable[..., None],
) -> dict:
    """校验 captcha、限流并发送 OTP；发送失败时保留此前仍有效的验证码。"""

    try:
        normalized_phone = normalize_phone(phone)
    except ValueError as err:
        raise PhoneOtpError(status_code=400, detail=str(err)) from None

    if not verify_captcha_fn(captcha_verify_param):
        raise PhoneOtpError(status_code=400, detail="验证码校验未通过")

    with _otp_send_lock_for(normalized_phone):
        count = count_verifications_last_hour(normalized_phone)
        if count >= int(max_per_phone_per_hour):
            raise PhoneOtpError(status_code=429, detail="发送频率过高，请稍后重试")

        code = generate_code_fn()
        verification = create_phone_verification(
            phone=normalized_phone,
            code=code,
            expires_minutes=int(expires_minutes),
        )
        try:
            send_code_fn(phone=normalized_phone, code=code)
        except Exception:
            invalidate_verification(verification["id"])
            logger.exception("sms: send failed for phone=%s", normalized_phone)
            raise PhoneOtpError(
                status_code=500,
                detail="短信发送失败，请稍后重试",
            ) from None

        invalidate_other_verifications_for_phone(
            normalized_phone,
            verification["id"],
        )
    return {"status": "ok"}


def verify_phone_otp(
    *,
    phone: str,
    code: str,
    token_expires_minutes: int,
) -> dict:
    """校验当前手机号 OTP，并签发可被产品注册流程单次消费的 verified token。"""

    try:
        normalized_phone = normalize_phone(phone)
    except ValueError as err:
        raise PhoneOtpError(status_code=400, detail=str(err)) from None

    verification = get_latest_active_verification(normalized_phone)
    if verification is None:
        raise PhoneOtpError(
            status_code=400,
            detail="验证码不存在或已过期，请重新获取",
        )
    if verification["verify_attempts"] >= 5:
        raise PhoneOtpError(status_code=400, detail="尝试次数过多，请重新获取验证码")
    if verification["code"] != code:
        increment_verify_attempts(verification["id"])
        raise PhoneOtpError(status_code=400, detail="验证码错误")

    result = set_verification_verified(
        verification["id"],
        token_expires_minutes=int(token_expires_minutes),
    )
    return {"status": "ok", "verified_token": result["verified_token"]}


__all__ = ["PhoneOtpError", "send_phone_otp", "verify_phone_otp"]
