"""渠道常量与渠道能力表（纯声明层，不引业务依赖）。

设计依据：``docs/architecture/shared/access/web_app_channel_access_design.md`` §4.1。

把接入层从「隐式假设微信」改成「渠道无关 + 渠道能力声明」。每个渠道用一个
``ChannelCapability`` 声明其短期会话隔离键、onboarding 开关、回复投递方式、是否可被
主动消息投递、是否接 TDAI 热召回。turn 核心按渠道 ``cap`` 参数化，而非到处硬编码
``channel == "openclaw-weixin"``。

**原则一（微信零影响）**：微信渠道的 cap 取值全部等价于现状（``__account_active__``、
onboarding=True、工具最终回复走网关 out-of-band、可被主动消息投递、TDAI 开），因此对
微信主链路是纯搬家，行为字节级不变。
"""

from __future__ import annotations

from dataclasses import dataclass


# 渠道常量。取值与 ``openclaw_gateway.DEFAULT_WEIXIN_CHANNEL`` 保持一致（微信入站/出站现状）。
CHANNEL_WEIXIN = "openclaw-weixin"
CHANNEL_WEB = "web"          # 本期新增（Phase 1 才有运行路径；Phase 0 仅声明）
# native = App 原生传输渠道。取值 "native" 以区分「App=产品层」与「channel=传输层」
# （见 app_account_convergence_and_channel_persona.md §9.2 Q5）。变量名保留 CHANNEL_APP，
# 存量 channel_bindings 的旧值 "app" 由迁移 m0024 一并改为 "native"；短期会话键
# _APP_ACTIVE_SESSION_KEY="__app_active__" 属会话隔离概念、与渠道名正交，不随之改。
CHANNEL_APP = "native"


# 微信短期会话隔离键，取值与 ``app.db._core.ACCOUNT_ACTIVE_SESSION_KEY`` 一致（不变）。
# 这里独立写一份字面量以避免 channels.py 反向依赖 db 层；两处取值必须相同。
_WEIXIN_ACTIVE_SESSION_KEY = "__account_active__"
_WEB_ACTIVE_SESSION_KEY = "__web_active__"
_APP_ACTIVE_SESSION_KEY = "__app_active__"


@dataclass(frozen=True)
class ChannelCapability:
    """单个渠道的能力声明（不可变）。

    字段含义见设计文档 §4.1。回复投递按现状拆三分（不能用单字段表达整个渠道）：
    普通回复走 ``default_reply_delivery``；工具最终回复是否可经网关 out-of-band 由
    ``supports_out_of_band_tool_final`` 决定（为 False 时**必须**同步返回、绝不调网关）。
    """

    active_session_key: str               # conversation_scope（短期会话隔离键，§7.1）
    onboarding_enabled: bool              # 是否走 onboarding 状态机（§4.1）
    onboarding_copy_key: str              # 文案键（渠道相关欢迎语）
    default_reply_delivery: str           # 普通回复投递方式："sync_http" | ...
    supports_out_of_band_tool_final: bool # 工具最终回复是否可经网关 out-of-band 发送
    supports_proactive: bool              # 是否可被主动消息投递；同时门控「产生未来投递」的工具（§8.3）
    tdai_enabled: bool                    # 是否接 TDAI 热召回/capture（§7.4）
    reply_presentation: str = "weixin"    # 回复呈现风格键（prompt_builder 选【回复呈现】文案）；
    #                                      默认 "weixin"=现状原文（原则一：未知渠道回落微信呈现）


# 未知渠道回落 cap：等价「现状微信传输，但不跑 onboarding」。这是**字节级等价现状**的关键——
# 现网 onboarding 门是 `identity.channel == "openclaw-weixin"` 的**精确**匹配，任何其它渠道
# （含缺省/未知）历史上都**不**走 onboarding，但短期会话键、工具集、TDAI、工具最终回复投递仍走
# 微信默认。故未知渠道必须是「微信 cap 除 onboarding 外」而非整份微信 cap（否则未知渠道会被错误
# 地拉进 onboarding，破坏原则一）。主动消息护栏（§8.3）不依赖此回落。
_DEFAULT_CAPABILITY = ChannelCapability(
    active_session_key=_WEIXIN_ACTIVE_SESSION_KEY,
    onboarding_enabled=False,
    onboarding_copy_key="weixin_welcome",
    default_reply_delivery="sync_http",
    supports_out_of_band_tool_final=True,
    supports_proactive=True,
    tdai_enabled=True,
)


CHANNELS = {
    # 微信：三种投递并存——普通回复同步 HTTP 返回；工具最终回复 & onboarding 走网关
    # out-of-band。取值全部等价现状（原则一）。
    CHANNEL_WEIXIN: ChannelCapability(
        active_session_key=_WEIXIN_ACTIVE_SESSION_KEY,
        onboarding_enabled=True,
        onboarding_copy_key="weixin_welcome",
        default_reply_delivery="sync_http",
        supports_out_of_band_tool_final=True,
        supports_proactive=True,
        tdai_enabled=True,
    ),
    # Web V1：全部同步 HTTP 返回，绝不进微信网关；已 onboarded 不引导；不可被主动消息
    # 投递（§8.3 护栏）；不接 TDAI（§7.4）。
    CHANNEL_WEB: ChannelCapability(
        active_session_key=_WEB_ACTIVE_SESSION_KEY,
        onboarding_enabled=False,
        onboarding_copy_key="web_welcome",
        default_reply_delivery="sync_http",
        supports_out_of_band_tool_final=False,
        supports_proactive=False,
        tdai_enabled=False,
        reply_presentation="web",
    ),
    # App V1：独立短期会话；普通回复和工具最终回复均由当前 HTTP 请求同步返回。
    # 不启用微信专用 onboarding，也不生成当前无法投递的主动任务。
    CHANNEL_APP: ChannelCapability(
        active_session_key=_APP_ACTIVE_SESSION_KEY,
        onboarding_enabled=False,
        onboarding_copy_key="app_welcome",
        default_reply_delivery="sync_http",
        supports_out_of_band_tool_final=False,
        supports_proactive=False,
        tdai_enabled=False,
        reply_presentation="native",
    ),
}


def get_channel_capability(channel: str) -> ChannelCapability:
    """按渠道名取能力声明。

    未知渠道回落到 ``_DEFAULT_CAPABILITY``（微信传输但 onboarding 关）——精确对齐现状：
    历史上只有 ``openclaw-weixin`` 精确匹配才走 onboarding，其它渠道走微信默认传输但不
    onboarding。故未知渠道按此回落，行为字节级不变（原则一）。主动消息护栏（§8.3）对未知
    渠道另有 ``supports_proactive`` 判定，不依赖此回落。
    """
    return CHANNELS.get(channel) or _DEFAULT_CAPABILITY
