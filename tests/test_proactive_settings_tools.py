"""Phase 1 主动消息设定：tool 定义 + handler + executor 路由。"""
from types import SimpleNamespace


from tests.factories import create_account as _create_account


def test_tools_registered_in_default_set():
    from app.products.zhaoxi.tools.registry import get_default_tools

    names = [t["function"]["name"] for t in get_default_tools()]
    assert "get_proactive_message_settings" in names
    assert "update_proactive_message_settings" in names


def test_get_handler_returns_defaults(fresh_db):
    from app.products.zhaoxi.tools.proactive_settings_handlers import handle_get_proactive_message_settings

    _create_account("acc-get")
    out = handle_get_proactive_message_settings({}, SimpleNamespace(account_id="acc-get"))
    assert out["status"] == "ok"
    assert out["settings"]["master_enabled"] is True
    assert "提醒不受影响" in out["summary"]


def test_update_handler_disables_master(fresh_db):
    from app.products.zhaoxi.tools.proactive_settings_handlers import handle_update_proactive_message_settings

    _create_account("acc-upd")
    out = handle_update_proactive_message_settings(
        {"master_enabled": False, "reason": "user_requested"},
        SimpleNamespace(account_id="acc-upd"),
    )
    assert out["status"] == "updated"
    assert out["settings"]["master_enabled"] is False
    assert "master_enabled" in out["changed_fields"]
    assert "整体关闭" in out["summary"]


def test_update_handler_ignores_forged_account_id(fresh_db):
    """patch/args 里伪造 account_id 必须被忽略，只用 ctx.account_id。"""
    from app.db import get_proactive_message_settings_row
    from app.products.zhaoxi.tools.proactive_settings_handlers import handle_update_proactive_message_settings

    _create_account("acc-real")
    _create_account("acc-victim")
    handle_update_proactive_message_settings(
        {"master_enabled": False, "account_id": "acc-victim"},
        SimpleNamespace(account_id="acc-real"),
    )
    # 受害账号没有被写入
    assert get_proactive_message_settings_row(account_id="acc-victim") is None
    real = get_proactive_message_settings_row(account_id="acc-real")
    assert real is not None and real["master_enabled"] is False


def test_update_handler_rejects_unknown_fields(fresh_db):
    from app.db import get_proactive_message_settings_row
    from app.products.zhaoxi.tools.proactive_settings_handlers import handle_update_proactive_message_settings

    _create_account("acc-unknown")
    out = handle_update_proactive_message_settings(
        {"master_enabled": False, "future_limit": 2},
        SimpleNamespace(account_id="acc-unknown"),
    )
    assert "error" in out
    assert "future_limit" in out["error"]
    assert get_proactive_message_settings_row(account_id="acc-unknown") is None


def test_update_handler_validation_error_returns_dict(fresh_db):
    from app.products.zhaoxi.tools.proactive_settings_handlers import handle_update_proactive_message_settings

    _create_account("acc-inval")
    out = handle_update_proactive_message_settings(
        {"quiet_hours": {"start": "99:99", "end": "08:00"}},
        SimpleNamespace(account_id="acc-inval"),
    )
    assert "error" in out


def test_update_handler_rejects_zero_total_per_day(fresh_db):
    from app.products.zhaoxi.tools.proactive_settings_handlers import handle_update_proactive_message_settings

    _create_account("acc-zero")
    out = handle_update_proactive_message_settings(
        {"total_per_day": 0},
        SimpleNamespace(account_id="acc-zero"),
    )
    assert out == {"error": "total_per_day 必须 >= 1"}


def test_update_handler_empty_patch(fresh_db):
    from app.products.zhaoxi.tools.proactive_settings_handlers import handle_update_proactive_message_settings

    _create_account("acc-empty")
    out = handle_update_proactive_message_settings(
        {"reason": "noop"}, SimpleNamespace(account_id="acc-empty")
    )
    assert "error" in out


def test_executor_routes_update_with_invocation_id(fresh_db):
    """executor 必须把 tool_invocation_id 透传给 update handler（用于审计关联）。"""
    from app.db import (
        create_tool_invocation,
        list_proactive_message_setting_events,
    )
    from app.tools.executor import execute_tool_call

    _create_account("acc-exec")
    invocation = create_tool_invocation(
        account_id="acc-exec",
        tool_name="update_proactive_message_settings",
        args={"master_enabled": False},
    )
    from app.products.zhaoxi.tools.registry import ZHAOXI_TOOL_POLICY

    ctx = SimpleNamespace(
        account_id="acc-exec",
        app_id="zhaoxi",
        tool_policy=ZHAOXI_TOOL_POLICY,
        web_search_enabled=False,
    )
    out = execute_tool_call(
        "update_proactive_message_settings",
        {"master_enabled": False},
        ctx,
        tool_invocation_id=invocation["id"],
    )
    assert out["status"] == "updated"
    events = list_proactive_message_setting_events(account_id="acc-exec")
    assert events and events[0]["tool_invocation_id"] == invocation["id"]


def test_executor_routes_get(fresh_db):
    from app.tools.executor import execute_tool_call

    _create_account("acc-execget")
    from app.products.zhaoxi.tools.registry import ZHAOXI_TOOL_POLICY

    ctx = SimpleNamespace(
        account_id="acc-execget",
        app_id="zhaoxi",
        tool_policy=ZHAOXI_TOOL_POLICY,
        web_search_enabled=False,
    )
    out = execute_tool_call("get_proactive_message_settings", {}, ctx)
    assert out["status"] == "ok"
