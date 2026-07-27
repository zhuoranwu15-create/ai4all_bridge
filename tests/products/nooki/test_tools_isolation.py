"""Nooki 与朝夕的 ToolPolicy 互相隔离：两边 catalog 零重名，跨产品调用一律"未知工具"。"""
from __future__ import annotations

from types import SimpleNamespace

from app.products.nooki.tools.registry import NOOKI_TOOL_CATALOG, NOOKI_TOOL_POLICY
from app.products.zhaoxi.tools.registry import ZHAOXI_TOOL_CATALOG, ZHAOXI_TOOL_POLICY
from app.tools.executor import execute_tool_call
from app.tools.registry import CALL_INVOCATION, CALL_PLAIN

_EXPECTED_NOOKI_TOOLS = {
    "nooki_create_task_with_options",
    "nooki_select_task_plan",
    "nooki_start_step",
    "nooki_complete_step",
    "nooki_shrink_step",
    "nooki_abandon_task",
    "nooki_list_state",
}


def _visible_names(policy) -> set[str]:
    if policy is ZHAOXI_TOOL_POLICY:
        tools = policy.get_default_tools(
            flags={"web_search_enabled": False, "tdai_search_enabled": False}
        )
    else:
        tools = policy.get_default_tools(flags={})
    return {schema["function"]["name"] for schema in tools}


def test_catalogs_share_no_tool_names():
    nooki_names = {spec.name for spec in NOOKI_TOOL_CATALOG.iter_specs()}
    zhaoxi_names = {spec.name for spec in ZHAOXI_TOOL_CATALOG.iter_specs()}
    assert nooki_names
    assert nooki_names == _EXPECTED_NOOKI_TOOLS
    assert zhaoxi_names
    assert nooki_names.isdisjoint(zhaoxi_names)


def test_zhaoxi_context_cannot_see_or_execute_nooki_tools():
    visible = _visible_names(ZHAOXI_TOOL_POLICY)
    assert "nooki_create_task_with_options" not in visible
    assert "nooki_start_step" not in visible

    ctx = SimpleNamespace(
        account_id="acc-zhaoxi",
        app_id="zhaoxi",
        tool_policy=ZHAOXI_TOOL_POLICY,
        web_search_enabled=False,
    )
    out = execute_tool_call("nooki_create_task_with_options", {}, ctx)
    assert out == {"error": "未知工具: nooki_create_task_with_options"}


def test_nooki_context_cannot_see_or_execute_zhaoxi_tools():
    visible = _visible_names(NOOKI_TOOL_POLICY)
    assert "create_reminder" not in visible
    assert "mission_status" not in visible

    ctx = SimpleNamespace(
        account_id="acc-nooki",
        app_id="nooki",
        tool_policy=NOOKI_TOOL_POLICY,
        web_search_enabled=False,
    )
    out = execute_tool_call("create_reminder", {}, ctx)
    assert out == {"error": "未知工具: create_reminder"}


def test_all_nooki_writes_use_invocation_call_style():
    for spec in NOOKI_TOOL_CATALOG.iter_specs():
        expected = CALL_PLAIN if spec.name == "nooki_list_state" else CALL_INVOCATION
        assert spec.call_style == expected
