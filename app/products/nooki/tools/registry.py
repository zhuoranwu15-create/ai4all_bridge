"""Nooki 产品 ToolPolicy：9 个目标拆解工具，P0+P1 不需要共享检索类工具。"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from app.bootstrap.product_registry import NOOKI_APP_ID
from app.products.nooki.tools.definitions import get_goal_breakdown_tools
from app.tools.registry import ToolBinding, ToolPolicy, ToolRegistry, build_specs

_HANDLER_MODULE = "app.products.nooki.tools.handlers"

_BINDINGS: Mapping[str, ToolBinding] = MappingProxyType(
    {
        "nooki_create_task_draft": ToolBinding(_HANDLER_MODULE, "handle_nooki_create_task_draft"),
        "nooki_create_step_options": ToolBinding(_HANDLER_MODULE, "handle_nooki_create_step_options"),
        "nooki_select_task_plan": ToolBinding(_HANDLER_MODULE, "handle_nooki_select_task_plan"),
        "nooki_start_step": ToolBinding(_HANDLER_MODULE, "handle_nooki_start_step"),
        "nooki_complete_step": ToolBinding(_HANDLER_MODULE, "handle_nooki_complete_step"),
        "nooki_shrink_step": ToolBinding(_HANDLER_MODULE, "handle_nooki_shrink_step"),
        "nooki_complete_task": ToolBinding(_HANDLER_MODULE, "handle_nooki_complete_task"),
        "nooki_abandon_task": ToolBinding(_HANDLER_MODULE, "handle_nooki_abandon_task"),
        "nooki_list_state": ToolBinding(_HANDLER_MODULE, "handle_nooki_list_state"),
    }
)

_GROUP_PROVIDERS = (("goal_breakdown", get_goal_breakdown_tools),)

NOOKI_TOOL_CATALOG = ToolRegistry(
    build_specs(group_providers=_GROUP_PROVIDERS, bindings=_BINDINGS)
)
NOOKI_TOOL_POLICY = ToolPolicy.allow_all(app_id=NOOKI_APP_ID, catalog=NOOKI_TOOL_CATALOG)


def get_default_tools():
    """Nooki 目前不做 flag/capability gating，全部工具始终可见。"""

    return NOOKI_TOOL_POLICY.get_default_tools()


__all__ = ["NOOKI_TOOL_CATALOG", "NOOKI_TOOL_POLICY", "get_default_tools"]
