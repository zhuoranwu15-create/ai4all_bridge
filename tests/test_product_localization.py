"""产品默认语言文案与公开错误码收敛。"""

import pytest

from app.products.zhaoxi.application.product_localization import (
    normalize_public_error_code,
    product_message,
)
from app.products.zhaoxi.application.turn_services import ZhaoxiTurnServices
from app.tools.errors import tool_error


@pytest.mark.parametrize(
    "language,expected",
    [
        ("zh-CN", "操作失败，请稍后重试。"),
        ("en-US", "Something went wrong. Please try again later."),
        ("ja-JP", "操作に失敗しました。しばらくしてからもう一度お試しください。"),
    ],
)
def test_product_message_supports_registered_languages(language, expected):
    assert product_message("generic_error", language=language) == expected


def test_product_message_unknown_key_falls_back_in_requested_language():
    assert product_message("missing_key", language="en-US") == (
        "Something went wrong. Please try again later."
    )


def test_product_message_interpolates_named_params():
    assert product_message("app_auth_welcome", language="en-US", name="Lumi") == (
        "Hi, I'm Lumi. Would you like to talk about how you feel right now, "
        "or just chat about anything?"
    )


def test_product_message_missing_param_falls_back_safely():
    assert product_message("app_auth_welcome", language="en-US") == (
        "Something went wrong. Please try again later."
    )


def test_tool_error_interpolates_params_in_chinese_fallback():
    result = tool_error(
        ZhaoxiTurnServices(),
        "reminder_days_exceeded",
        fallback="最多支持 {max_days} 天",
        max_days=30,
    )

    assert result == {
        "error_code": "reminder_days_exceeded",
        "error": "最多支持 30 天",
    }


def test_localized_message_keeps_fallback_when_param_is_missing():
    assert ZhaoxiTurnServices().localized_message(
        "reminder_days_exceeded",
        fallback="最多支持 {max_days} 天",
    ) == "最多支持 {max_days} 天"


def test_localized_message_forwards_params_to_non_chinese_catalog(monkeypatch):
    monkeypatch.setattr(
        "app.products.zhaoxi.application.turn_services.product_default_language",
        lambda _app_id: "en-US",
    )

    assert ZhaoxiTurnServices().localized_message(
        "app_auth_welcome",
        fallback="你好，我是{name}。",
        name="Lumi",
    ) == (
        "Hi, I'm Lumi. Would you like to talk about how you feel right now, "
        "or just chat about anything?"
    )


@pytest.mark.parametrize(
    "detail,status_code,expected",
    [
        ("验证码错误", 400, "otp_invalid"),
        ("phone must be a valid Chinese mobile number (1[3-9]XXXXXXXXX)", 400, "invalid_phone"),
        ("creator_role_template_not_found", 404, "creator_role_template_not_found"),
        ("provider leaked secret text", 502, "service_unavailable"),
    ],
)
def test_normalize_public_error_code_never_exposes_long_detail(detail, status_code, expected):
    assert normalize_public_error_code(detail, status_code=status_code) == expected
