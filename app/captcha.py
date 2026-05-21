import logging

from app.config import settings

logger = logging.getLogger("ai4all.captcha")

_client = None


def _get_client():
    global _client
    if _client is None:
        from alibabacloud_captcha20230305.client import Client
        from alibabacloud_tea_openapi import models as open_api_models

        config = open_api_models.Config(
            access_key_id=settings.aliyun_access_key_id,
            access_key_secret=settings.aliyun_access_key_secret,
        )
        config.endpoint = "captcha.cn-shanghai.aliyuncs.com"
        _client = Client(config)
    return _client


def verify_captcha(captcha_verify_param: str) -> bool:
    """Verify an Aliyun Captcha 2.0 token.

    Returns True if the captcha passed.
    If ALIYUN_CAPTCHA_SCENE_ID is empty, skips the API call and returns True (mock mode).
    captcha_verify_param must be passed through from the client unmodified.
    """
    if not settings.aliyun_captcha_scene_id:
        logger.info("captcha: mock mode (no scene_id), auto-pass")
        return True

    from alibabacloud_captcha20230305 import models as captcha_models

    client = _get_client()
    request = captcha_models.VerifyIntelligentCaptchaRequest(
        captcha_verify_param=captcha_verify_param,
        scene_id=settings.aliyun_captcha_scene_id,
    )
    response = client.verify_intelligent_captcha(request)
    result = response.body.result
    logger.info(
        "captcha: verify_result=%s verify_code=%s",
        result.verify_result,
        result.verify_code,
    )
    return bool(result.verify_result)
