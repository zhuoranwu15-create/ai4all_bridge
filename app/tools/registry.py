"""工具注册表：每个工具的 schema、handler 与分发元数据的**单一事实源**。

历史上 schema 在 definitions.py、handler 映射在 executor.py 各写一份，工具名两边手抄、
靠人工保持一致，漏改即出现"模型看得到但后端 unknown tool"或反之。这里把两者绑定在一处：

- schema 仍由 definitions.get_*_tools() 提供（保持不动）；
- 每个工具名 → (handler 模块/函数、调用风格、运行时开关、默认集开关) 在 _META 集中声明；
- import 时**双向校验**：有 schema 无 _META、或有 _META 无 schema、或重名，都直接抛错（把
  "静默漂移"变成启动期可见的崩溃）。

handler 用"模块路径 + 函数名"惰性引用，按需 import，避免 handler 模块（依赖 app.db /
app.proactive 等）在工具包加载时引入循环导入——这也是 executor 历史上惰性 import 的原因。
"""
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.tools.definitions import (
    get_content_invitation_generation_tools,
    get_content_invitation_response_tools,
    get_proactive_message_settings_tools,
    get_read_tools,
    get_reminder_tools,
    get_session_status_tools,
    get_web_fetch_tools,
    get_web_search_tools,
)

# 调用风格：handler 的入参形态。
CALL_PLAIN = "plain"            # handler(args, ctx)
CALL_INVOCATION = "invocation"  # handler(args, ctx, *, tool_invocation_id=...)
CALL_WEB_SEARCH = "web_search"  # handler(args, ctx, *, tool_call_id=..., tool_invocation_id=...)

# default_when_flag 取值：None=始终进默认工具集；"never"=不进默认集（仅特定路径直接装载，
# 但仍可被 execute_tool_call 分发）；其它=按该 ctx/flag 名 gating。
_DEFAULT_ALWAYS = None
_DEFAULT_NEVER = "never"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    schema: dict
    group: str
    handler_module: str
    handler_attr: str
    call_style: str = CALL_PLAIN
    # 运行时开关：非空且 ctx 上该属性为假时，拒绝执行（如 web_search 被禁用）。
    runtime_requires_flag: Optional[str] = None
    # 默认工具集 gating（见上）。
    default_when_flag: Optional[str] = _DEFAULT_ALWAYS


# 每个工具的分发元数据：name -> (handler_module, handler_attr, call_style,
# runtime_requires_flag, default_when_flag)。这是分发与默认集的单一声明处。
_META: Dict[str, tuple] = {
    "web_fetch": (
        "app.tools.web_fetch_handlers", "handle_web_fetch",
        CALL_PLAIN, None, _DEFAULT_ALWAYS,
    ),
    "read": (
        "app.tools.read_handlers", "handle_read",
        CALL_PLAIN, None, _DEFAULT_ALWAYS,
    ),
    "create_reminder": ("app.tools.reminder_handlers", "handle_create_reminder", CALL_PLAIN, None, _DEFAULT_ALWAYS),
    "list_reminders": ("app.tools.reminder_handlers", "handle_list_reminders", CALL_PLAIN, None, _DEFAULT_ALWAYS),
    "cancel_reminder": ("app.tools.reminder_handlers", "handle_cancel_reminder", CALL_PLAIN, None, _DEFAULT_ALWAYS),
    "update_reminder": ("app.tools.reminder_handlers", "handle_update_reminder", CALL_PLAIN, None, _DEFAULT_ALWAYS),
    "session_status": ("app.tools.session_status_handlers", "handle_session_status", CALL_PLAIN, None, _DEFAULT_ALWAYS),
    "get_proactive_message_settings": (
        "app.tools.proactive_settings_handlers", "handle_get_proactive_message_settings",
        CALL_PLAIN, None, _DEFAULT_ALWAYS,
    ),
    "update_proactive_message_settings": (
        "app.tools.proactive_settings_handlers", "handle_update_proactive_message_settings",
        CALL_INVOCATION, None, _DEFAULT_ALWAYS,
    ),
    "web_search": (
        "app.tools.web_search_handlers", "handle_web_search",
        CALL_WEB_SEARCH, "web_search_enabled", "web_search_enabled",
    ),
    "send_content_invitation_titles": (
        "app.tools.content_invitation_handlers", "handle_send_content_invitation_titles",
        CALL_INVOCATION, None, "content_invitation_response_enabled",
    ),
    "record_content_invitation_feedback": (
        "app.tools.content_invitation_handlers", "handle_record_content_invitation_feedback",
        CALL_INVOCATION, None, "content_invitation_response_enabled",
    ),
    "create_content_invitation_candidate": (
        "app.tools.content_invitation_handlers", "handle_create_content_invitation_candidate",
        CALL_PLAIN, None, _DEFAULT_NEVER,
    ),
    "skip_content_invitation": (
        "app.tools.content_invitation_handlers", "handle_skip_content_invitation",
        CALL_PLAIN, None, _DEFAULT_NEVER,
    ),
}

# 分组 → schema 提供函数。顺序即默认工具集的装配顺序（保持与历史 get_default_tools 一致：
# reminder → session_status → proactive_message_settings → web_search → content_invitation_response），
# content_invitation_generation 仅供生成路径直接装载，放最后且永不进默认集。
_GROUP_PROVIDERS: List[tuple] = [
    ("web_fetch", get_web_fetch_tools),
    ("read", get_read_tools),
    ("reminder", get_reminder_tools),
    ("session_status", get_session_status_tools),
    ("proactive_message_settings", get_proactive_message_settings_tools),
    ("web_search", get_web_search_tools),
    ("content_invitation_response", get_content_invitation_response_tools),
    ("content_invitation_generation", get_content_invitation_generation_tools),
]


def _build_registry() -> List[ToolSpec]:
    specs: List[ToolSpec] = []
    seen: set = set()
    for group, provider in _GROUP_PROVIDERS:
        for schema in provider():
            name = schema["function"]["name"]
            if name in seen:
                raise RuntimeError(f"重复的工具名: {name}")
            seen.add(name)
            meta = _META.get(name)
            if meta is None:
                raise RuntimeError(f"工具 schema '{name}' 在 registry._META 缺少分发元数据")
            module, attr, call_style, runtime_flag, default_flag = meta
            specs.append(
                ToolSpec(
                    name=name,
                    schema=schema,
                    group=group,
                    handler_module=module,
                    handler_attr=attr,
                    call_style=call_style,
                    runtime_requires_flag=runtime_flag,
                    default_when_flag=default_flag,
                )
            )
    missing = set(_META) - seen
    if missing:
        raise RuntimeError(f"registry._META 存在没有对应 schema 的工具: {sorted(missing)}")
    return specs


_SPECS: List[ToolSpec] = _build_registry()
_BY_NAME: Dict[str, ToolSpec] = {s.name: s for s in _SPECS}


def iter_specs() -> List[ToolSpec]:
    """返回所有工具规格（注册顺序）。"""
    return list(_SPECS)


def get_spec(name: str) -> Optional[ToolSpec]:
    """按工具名取规格；未注册返回 None。"""
    return _BY_NAME.get(name)


def get_default_tools(
    *,
    web_search_enabled: bool = False,
    content_invitation_response_enabled: bool = False,
) -> list:
    """主对话默认工具集：按 default_when_flag gating 后返回 schema 列表（顺序稳定）。"""
    flags = {
        "web_search_enabled": web_search_enabled,
        "content_invitation_response_enabled": content_invitation_response_enabled,
    }
    out = []
    for spec in _SPECS:
        flag = spec.default_when_flag
        if flag is _DEFAULT_ALWAYS:
            include = True
        elif flag == _DEFAULT_NEVER:
            include = False
        else:
            include = bool(flags.get(flag))
        if include:
            out.append(spec.schema)
    return out
