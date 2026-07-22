"""渠道能力表（app/channels.py）单测。

核心断言：微信 cap 取值等价现状（原则一）；web cap 为保守值；未知渠道回落「微信传输但
不 onboarding」的默认 cap（精确对齐现状：只有 openclaw-weixin 精确匹配才走 onboarding）。
"""

from app.channels import (
    _DEFAULT_CAPABILITY,
    _APP_ACTIVE_SESSION_KEY,
    CHANNEL_APP,
    CHANNEL_WEB,
    CHANNEL_WEIXIN,
    CHANNELS,
    ChannelCapability,
    get_channel_capability,
)
from app.db import ACCOUNT_ACTIVE_SESSION_KEY, APP_ACTIVE_SESSION_KEY


def test_weixin_capability_equivalent_to_current_behavior():
    cap = CHANNELS[CHANNEL_WEIXIN]
    # active session key 必须与 db 层现状常量一致（scope 参数化后微信取值不变）。
    assert cap.active_session_key == ACCOUNT_ACTIVE_SESSION_KEY == "__account_active__"
    assert cap.onboarding_enabled is True
    assert cap.onboarding_copy_key == "weixin_welcome"
    assert cap.default_reply_delivery == "sync_http"
    assert cap.supports_out_of_band_tool_final is True
    assert cap.supports_proactive is True
    assert cap.tdai_enabled is True


def test_web_capability_conservative_defaults():
    cap = CHANNELS[CHANNEL_WEB]
    assert cap.active_session_key == "__web_active__"
    assert cap.onboarding_enabled is False
    assert cap.default_reply_delivery == "sync_http"
    # Web 绝不走网关 out-of-band、不可被主动消息投递、不接 TDAI。
    assert cap.supports_out_of_band_tool_final is False
    assert cap.supports_proactive is False
    assert cap.tdai_enabled is False


def test_app_capability_is_independent_and_sync_only():
    cap = CHANNELS[CHANNEL_APP]
    # channels 与 db 各保留一份字面量来避免循环依赖，必须由测试锁住一致性。
    assert cap.active_session_key == _APP_ACTIVE_SESSION_KEY == APP_ACTIVE_SESSION_KEY
    assert cap.onboarding_enabled is False
    assert cap.default_reply_delivery == "sync_http"
    assert cap.supports_out_of_band_tool_final is False
    assert cap.supports_proactive is False
    assert cap.tdai_enabled is False


def test_channel_app_renamed_to_native():
    """§9.2 Q5:App 原生渠道值改名 native（区分 App=产品层 / channel=传输层）；
    会话隔离键仍为 __app_active__（正交，不随渠道名改）。"""
    assert CHANNEL_APP == "native"
    assert CHANNELS[CHANNEL_APP].active_session_key == "__app_active__"
    assert get_channel_capability("native") is CHANNELS[CHANNEL_APP]


def test_reply_presentation_per_channel():
    """L3:weixin 呈现档=weixin（原则一），native/web 为各自档，未知渠道回落 weixin。"""
    assert CHANNELS[CHANNEL_WEIXIN].reply_presentation == "weixin"
    assert CHANNELS[CHANNEL_WEB].reply_presentation == "web"
    assert CHANNELS[CHANNEL_APP].reply_presentation == "native"
    assert _DEFAULT_CAPABILITY.reply_presentation == "weixin"


def test_capability_is_frozen():
    cap = CHANNELS[CHANNEL_WEIXIN]
    assert isinstance(cap, ChannelCapability)
    try:
        cap.supports_proactive = False  # type: ignore[misc]
    except Exception as err:
        assert type(err).__name__ in {"FrozenInstanceError", "AttributeError"}
    else:
        raise AssertionError("ChannelCapability 应为不可变（frozen）")


def test_get_channel_capability_known():
    assert get_channel_capability(CHANNEL_WEIXIN) is CHANNELS[CHANNEL_WEIXIN]
    assert get_channel_capability(CHANNEL_WEB) is CHANNELS[CHANNEL_WEB]


def test_get_channel_capability_unknown_falls_back_to_default():
    # 未知渠道回落默认 cap：微信传输取值，但 onboarding 关（现状只有 openclaw-weixin
    # 精确匹配才 onboarding）。这是字节级等价现状的关键，见 _DEFAULT_CAPABILITY 注释。
    assert get_channel_capability("totally-unknown") is _DEFAULT_CAPABILITY
    assert get_channel_capability("") is _DEFAULT_CAPABILITY
    # 默认 cap = 微信传输但不 onboarding。
    assert _DEFAULT_CAPABILITY.onboarding_enabled is False
    assert _DEFAULT_CAPABILITY.active_session_key == ACCOUNT_ACTIVE_SESSION_KEY
    assert _DEFAULT_CAPABILITY.supports_out_of_band_tool_final is True
    assert _DEFAULT_CAPABILITY.supports_proactive is True
    assert _DEFAULT_CAPABILITY.tdai_enabled is True
