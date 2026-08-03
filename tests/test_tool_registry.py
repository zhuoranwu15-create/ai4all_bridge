"""P1-3：工具注册表一致性与 gating 测试。

注册表是 schema↔handler 的单一事实源：这里锁定双向一致（每个 schema 有 handler 绑定、
每个 _META 有 schema）、默认集 gating、生成类工具不进默认集但仍可分发、web_search 运行时
开关，避免后续加工具时再出现"模型看得到但后端 unknown"或反之的静默漂移。
"""
from types import SimpleNamespace

import pytest

from app.products.zhaoxi.tools.registry import (
    ZHAOXI_TOOL_CATALOG,
    ZHAOXI_TOOL_POLICY,
    _META,
    get_default_tools,
    get_spec,
    iter_specs,
)
from app.tools.registry import (
    CALL_INVOCATION,
    CALL_PLAIN,
    CALL_WEB_SEARCH,
    SHARED_TOOL_REGISTRY,
    ToolPolicy,
)
from app.tools.executor import execute_tool_call


def test_registry_schema_and_meta_are_bijective():
    spec_names = {s.name for s in iter_specs()}
    assert spec_names == set(_META), "schema 与 _META 必须一一对应（无单边定义）"
    # 无重名
    assert len(spec_names) == len(iter_specs())


def test_every_spec_has_resolvable_handler():
    import importlib

    for spec in iter_specs():
        mod = importlib.import_module(spec.handler_module)
        assert hasattr(mod, spec.handler_attr), (
            f"{spec.name} 的 handler {spec.handler_module}.{spec.handler_attr} 不存在"
        )
        assert spec.call_style in {CALL_PLAIN, CALL_INVOCATION, CALL_WEB_SEARCH}


def test_default_set_gating():
    base = {t["function"]["name"] for t in get_default_tools()}
    # 始终在默认集
    assert {"create_reminder", "session_status", "update_proactive_message_settings"} <= base
    # gating：未开启时不在
    assert "web_search" not in base
    assert "send_content_invitation_titles" not in base
    # 生成类工具永不进默认集
    assert "create_content_invitation_candidate" not in base
    assert "skip_content_invitation" not in base

    full = {
        t["function"]["name"]
        for t in get_default_tools(
            web_search_enabled=True, content_invitation_response_enabled=True
        )
    }
    assert "web_search" in full
    assert {"send_content_invitation_titles", "record_content_invitation_feedback"} <= full
    # 即便开启所有 flag，生成类工具也不进默认集
    assert "create_content_invitation_candidate" not in full


def test_generation_tools_excluded_from_default_but_dispatchable():
    # 不在默认集，但注册表里可查到（供生成路径直接装载 + execute_tool_call 分发）。
    assert get_spec("create_content_invitation_candidate") is not None
    assert get_spec("skip_content_invitation") is not None


def test_execute_unknown_tool_returns_error():
    ctx = SimpleNamespace(
        account_id="acc-x",
        app_id="zhaoxi",
        tool_policy=ZHAOXI_TOOL_POLICY,
        web_search_enabled=False,
    )
    out = execute_tool_call("definitely_not_a_tool", {}, ctx)
    assert out == {
        "error_code": "tool_unknown",
        "error": "未知工具",
        "tool_name": "definitely_not_a_tool",
    }


def test_execute_web_search_disabled_runtime_guard():
    ctx = SimpleNamespace(
        account_id="acc-x",
        app_id="zhaoxi",
        tool_policy=ZHAOXI_TOOL_POLICY,
        web_search_enabled=False,
    )
    out = execute_tool_call("web_search", {"query": "x"}, ctx)
    assert out == {
        "status": "failed",
        "error_code": "tool_disabled",
        "error": "tool is disabled",
        "tool_name": "web_search",
    }


def test_test_product_cannot_see_or_execute_zhaoxi_tools():
    policy = ToolPolicy(
        app_id="test_product",
        catalog=ZHAOXI_TOOL_CATALOG,
        allowed_names=SHARED_TOOL_REGISTRY.names(),
    )
    visible = {
        schema["function"]["name"]
        for schema in policy.get_default_tools(
            flags={"web_search_enabled": False, "tdai_search_enabled": False}
        )
    }
    assert "create_reminder" not in visible
    assert "mission_status" not in visible

    ctx = SimpleNamespace(
        account_id="acc-test",
        app_id="test_product",
        tool_policy=policy,
        web_search_enabled=False,
    )
    out = execute_tool_call("create_reminder", {}, ctx)
    assert out == {
        "status": "failed",
        "error_code": "tool_not_allowed",
        "error": "tool is not allowed for this product",
        "tool_name": "create_reminder",
    }
