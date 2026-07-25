"""朝夕产品 ToolPolicy、schema 顺序与 handler metadata。"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import Mapping

from app.bootstrap.product_registry import ZHAOXI_APP_ID
from app.products.zhaoxi.tools.definitions import (
    get_commitment_tools,
    get_content_invitation_generation_tools,
    get_content_invitation_response_tools,
    get_mission_tools,
    get_proactive_message_settings_tools,
    get_reminder_tools,
    get_session_status_tools,
)
from app.tools.definitions import (
    get_read_tools,
    get_tdai_search_tools,
    get_web_fetch_tools,
    get_web_search_tools,
)
from app.tools.registry import (
    CALL_INVOCATION,
    DEFAULT_NEVER,
    SHARED_TOOL_BINDINGS,
    ToolBinding,
    ToolPolicy,
    ToolRegistry,
    build_specs,
)

_PROACTIVE_COUNT_RE = r"(?:[0-9０-９]+|[一二两三四五六七八九十]+)"
_PROACTIVE_UPDATE_RE = re.compile(
    rf"(每天|每日|一天|总共|一周|每周)\s*(最多|最少|只|就)?\s*发?\s*{_PROACTIVE_COUNT_RE}\s*(条|次)"
    rf"|(条数|上限|频次).{{0,6}}(改为|设为|调整为|改|设|调|限|调整).{{0,8}}{_PROACTIVE_COUNT_RE}"
    rf"|(改为|设为|调整为|改成|设成).{{0,6}}{_PROACTIVE_COUNT_RE}.{{0,4}}(条|次)"
    rf"|{_PROACTIVE_COUNT_RE}.{{0,5}}(条|次).{{0,8}}(就够|就行|为限|上限|够了)"
    r"|别(再|继续)?(主动|发).{0,10}(消息|找|发)"
    r"|(关掉?|开启?|暂停|停止|恢复).{0,6}主动"
    r"|主动消息.{0,10}(关闭|开启|暂停|停止|修改|调整|改为|设为|限制|减少|增加)"
    r"|(?:主动消息|主动|找我|联系我).{0,8}(?:多|少)发.{0,4}(?:点|些|次|条)"
    r"|(?:多|少)发.{0,4}(?:点|些|次|条).{0,8}(?:主动消息|主动找我|找我|联系我)"
    r"|总(共|量).{0,8}(条数|上限|改为|设为|[0-9０-９])",
    re.IGNORECASE,
)


def _first_round_tool_choice(user_text: str):
    """按朝夕主动设置意图选择首轮工具。"""

    if _PROACTIVE_UPDATE_RE.search(str(user_text or "")):
        return {
            "type": "function",
            "function": {"name": "update_proactive_message_settings"},
        }
    return "auto"


_PRODUCT_BINDINGS: Mapping[str, ToolBinding] = MappingProxyType(
    {
        "create_reminder": ToolBinding(
            "app.products.zhaoxi.tools.reminder_handlers",
            "handle_create_reminder",
            capability_requires="supports_proactive",
        ),
        "create_commitment": ToolBinding(
            "app.products.zhaoxi.tools.commitment_handlers",
            "handle_create_commitment",
            capability_requires="supports_proactive",
        ),
        "list_reminders": ToolBinding(
            "app.products.zhaoxi.tools.reminder_handlers",
            "handle_list_reminders",
            capability_requires="supports_proactive",
        ),
        "cancel_reminder": ToolBinding(
            "app.products.zhaoxi.tools.reminder_handlers",
            "handle_cancel_reminder",
            capability_requires="supports_proactive",
        ),
        "update_reminder": ToolBinding(
            "app.products.zhaoxi.tools.reminder_handlers",
            "handle_update_reminder",
            capability_requires="supports_proactive",
        ),
        "session_status": ToolBinding(
            "app.products.zhaoxi.tools.session_status_handlers",
            "handle_session_status",
        ),
        "mission_status": ToolBinding(
            "app.products.zhaoxi.tools.mission_handlers",
            "handle_mission_status",
            default_when_flag="has_mission",
            enabled_reason="has_mission",
            disabled_reason="no_mission_assigned",
        ),
        "record_mission_moment": ToolBinding(
            "app.products.zhaoxi.tools.mission_handlers",
            "handle_record_mission_moment",
            call_style=CALL_INVOCATION,
            default_when_flag="has_mission",
            enabled_reason="has_mission",
            disabled_reason="no_mission_assigned",
        ),
        "get_proactive_message_settings": ToolBinding(
            "app.products.zhaoxi.tools.proactive_settings_handlers",
            "handle_get_proactive_message_settings",
        ),
        "update_proactive_message_settings": ToolBinding(
            "app.products.zhaoxi.tools.proactive_settings_handlers",
            "handle_update_proactive_message_settings",
            call_style=CALL_INVOCATION,
        ),
        "send_content_invitation_titles": ToolBinding(
            "app.products.zhaoxi.tools.content_invitation_handlers",
            "handle_send_content_invitation_titles",
            call_style=CALL_INVOCATION,
            default_when_flag="content_invitation_response_enabled",
            enabled_reason="active_content_invitation",
            disabled_reason="no_active_content_invitation",
        ),
        "record_content_invitation_feedback": ToolBinding(
            "app.products.zhaoxi.tools.content_invitation_handlers",
            "handle_record_content_invitation_feedback",
            call_style=CALL_INVOCATION,
            default_when_flag="content_invitation_response_enabled",
            enabled_reason="active_content_invitation",
            disabled_reason="no_active_content_invitation",
        ),
        "create_content_invitation_candidate": ToolBinding(
            "app.products.zhaoxi.tools.content_invitation_handlers",
            "handle_create_content_invitation_candidate",
            default_when_flag=DEFAULT_NEVER,
        ),
        "skip_content_invitation": ToolBinding(
            "app.products.zhaoxi.tools.content_invitation_handlers",
            "handle_skip_content_invitation",
            default_when_flag=DEFAULT_NEVER,
        ),
    }
)

_META = MappingProxyType({**SHARED_TOOL_BINDINGS, **_PRODUCT_BINDINGS})
_GROUP_PROVIDERS = (
    ("web_fetch", get_web_fetch_tools),
    ("read", get_read_tools),
    ("reminder", get_reminder_tools),
    ("commitment", get_commitment_tools),
    ("session_status", get_session_status_tools),
    ("mission", get_mission_tools),
    ("proactive_message_settings", get_proactive_message_settings_tools),
    ("web_search", get_web_search_tools),
    ("tdai_search", get_tdai_search_tools),
    ("content_invitation_response", get_content_invitation_response_tools),
    ("content_invitation_generation", get_content_invitation_generation_tools),
)

ZHAOXI_TOOL_CATALOG = ToolRegistry(
    build_specs(group_providers=_GROUP_PROVIDERS, bindings=_META)
)
ZHAOXI_TOOL_POLICY = ToolPolicy.allow_all(
    app_id=ZHAOXI_APP_ID,
    catalog=ZHAOXI_TOOL_CATALOG,
    first_round_choice_resolver=_first_round_tool_choice,
)


def iter_specs():
    """返回全部朝夕工具规格。"""

    return ZHAOXI_TOOL_POLICY.iter_specs()


def get_spec(name: str):
    """查询朝夕允许的工具规格。"""

    return ZHAOXI_TOOL_POLICY.get_spec(name)


def get_default_tools(
    *,
    web_search_enabled: bool = False,
    content_invitation_response_enabled: bool = False,
    has_mission: bool = False,
    tdai_search_enabled: bool = False,
    supports_proactive: bool = True,
):
    """按朝夕 turn flags 和渠道能力返回模型默认工具集合。"""

    return ZHAOXI_TOOL_POLICY.get_default_tools(
        flags={
            "web_search_enabled": web_search_enabled,
            "content_invitation_response_enabled": content_invitation_response_enabled,
            "has_mission": has_mission,
            "tdai_search_enabled": tdai_search_enabled,
        },
        capabilities={"supports_proactive": supports_proactive},
    )


__all__ = [
    "ZHAOXI_TOOL_CATALOG",
    "ZHAOXI_TOOL_POLICY",
    "get_default_tools",
    "get_spec",
    "iter_specs",
]
