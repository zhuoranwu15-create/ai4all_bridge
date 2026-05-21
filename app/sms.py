import json
import logging
import secrets

from app.config import settings

logger = logging.getLogger("ai4all.sms")

_client = None


def _get_client():
    global _client
    if _client is None:
        from alibabacloud_dysmsapi20170525.client import Client
        from alibabacloud_tea_openapi import models as open_api_models

        config = open_api_models.Config(
            access_key_id=settings.aliyun_access_key_id,
            access_key_secret=settings.aliyun_access_key_secret,
        )
        config.endpoint = "dysmsapi.aliyuncs.com"
        _client = Client(config)
    return _client


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def send_otp(phone: str, code: str) -> None:
    """Send an OTP SMS via Aliyun.

    If ALIYUN_ACCESS_KEY_ID is empty, logs the code instead of calling the API (mock mode).
    Raises RuntimeError if the Aliyun API returns a non-OK response code.
    """
    if not settings.aliyun_access_key_id:
        if settings.app_env not in ("local", "test"):
            raise RuntimeError("SMS credentials not configured in production environment")
        logger.info("sms: mock mode (no credentials), otp=%s phone=%s", code, phone)
        return

    from alibabacloud_dysmsapi20170525 import models as sms_models

    client = _get_client()
    request = sms_models.SendSmsRequest(
        phone_numbers=phone,
        sign_name=settings.aliyun_sms_sign_name,
        template_code=settings.aliyun_sms_template_code,
        template_param=json.dumps({"code": code}),
    )
    response = client.send_sms(request)
    body = response.body
    logger.info(
        "sms: result code=%s message=%s biz_id=%s",
        body.code,
        body.message,
        body.biz_id,
    )
    if body.code != "OK":
        raise RuntimeError(f"SMS send failed: {body.code} {body.message}")
