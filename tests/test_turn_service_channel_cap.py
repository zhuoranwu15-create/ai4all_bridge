"""Slice 6：turn_service 渠道能力参数化守卫。

核心不变量（原则一）：微信 cap（supports_proactive=True 等全 True）下工具集与历史逐字节一致；
仅当渠道能力保守（如 Web V1：supports_proactive=False）时才裁剪「产生未来投递」工具。
"""
from app.channels import CHANNEL_WEIXIN, ChannelCapability, get_channel_capability
from app.tools import get_default_tools
from app.turn_service import _PROACTIVE_DELIVERY_TOOLS, _build_tooling_envelope, _tool_name


def _envelope(cap: ChannelCapability, *, onboarding_active: bool = False):
    return _build_tooling_envelope(
        onboarding_active=onboarding_active,
        web_search_enabled=False,
        active_content_invitation=None,
        has_mission=False,
        text="你好",
        include_tool_instructions=True,
        cap=cap,
    )


def test_weixin_cap_tool_set_byte_equivalent_to_history():
    """微信 cap（supports_proactive=True）不裁剪：工具集 == 历史 get_default_tools 原样。"""
    cap = get_channel_capability(CHANNEL_WEIXIN)
    assert cap.supports_proactive is True
    env = _envelope(cap)
    expected = [
        _tool_name(s)
        for s in get_default_tools(
            web_search_enabled=False,
            content_invitation_response_enabled=False,
            has_mission=False,
        )
    ]
    assert env["available_tool_names"] == expected
    # 五个「产生未来投递」工具在微信侧全部保留。
    assert _PROACTIVE_DELIVERY_TOOLS <= set(env["available_tool_names"])


def test_non_proactive_cap_drops_future_delivery_tools():
    """保守渠道（supports_proactive=False）剔除提醒/承诺类工具，其余工具不受影响。"""
    web_like = ChannelCapability(
        active_session_key="__web_active__",
        onboarding_enabled=False,
        onboarding_copy_key="web_welcome",
        default_reply_delivery="sync_http",
        supports_out_of_band_tool_final=False,
        supports_proactive=False,
        tdai_enabled=False,
    )
    names = set(_envelope(web_like)["available_tool_names"])
    # 五个投递类工具被剔除。
    assert not (_PROACTIVE_DELIVERY_TOOLS & names)
    # 非投递类工具（读/取网页/会话状态/主动设置查改）仍在。
    assert {"read", "web_fetch", "session_status", "get_proactive_message_settings"} <= names
    # disabled_tools 里能看到被剔除的提醒工具（供调试可见，不是静默消失）。
    disabled_names = {item["name"] for item in _envelope(web_like)["disabled_tools"]}
    assert "create_reminder" in disabled_names


def test_onboarding_active_ignores_cap_and_stays_plain():
    """onboarding 期无论何种 cap 都是 plain 模式、无工具（cap 裁剪不改变这一点）。"""
    cap = get_channel_capability(CHANNEL_WEIXIN)
    env = _envelope(cap, onboarding_active=True)
    assert env["mode"] == "plain"
    assert env["tools"] == []
    assert env["available_tool_names"] == []
