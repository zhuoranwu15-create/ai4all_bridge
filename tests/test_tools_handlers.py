from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest

from app.agent_runtime.context.models import TurnContext


def _make_ctx(account_id="acc-tool"):
    identity = MagicMock()
    identity.channel = "openclaw-weixin"
    identity.channel_account_id = "bot-1"
    identity.chat_id = "chat-1"
    identity.session_key = "sk-1"
    return TurnContext(
        account_id=account_id,
        app_id="zhaoxi",
        account={"id": account_id},
        session={"id": 1},
        identity=identity,
        binding={"id": 1, "chat_id": "chat-1"},
        message_id="msg-1",
        text="测试",
        today="2026-05-30",
        business_day="2026-05-30",
        profile_path=None,
        debug_trace_enabled=False,
        onboarding_state="complete",
        onboarding_active=False,
        recent_messages=[],
        background_loop=None,
    )


def _setup_account(account_id: str) -> None:
    from app.db import get_or_create_session
    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="s",
        sender_name=None,
        chat_id="chat-1",
        session_key=f"sk-{account_id}",
    )


def test_handle_create_reminder_one_shot(fresh_db):
    from app.products.zhaoxi.tools.reminder_handlers import handle_create_reminder
    with patch("app.db.settings", fresh_db):
        _setup_account("acc-tool")
    ctx = _make_ctx()
    with patch("app.db.settings", fresh_db):
        result = handle_create_reminder(
            {"text": "检查事情A", "due_at": "2026-06-01 10:00:00"},
            ctx,
        )
    assert result["status"] == "created"
    assert "reminder_id" in result
    assert result["recur_rule"] is None


def test_handle_create_reminder_dynamic_blocked_when_disabled(fresh_db):
    """总开关关闭 → 拒绝创建 dynamic 提醒（唯一的启停闸）。"""
    from app.products.zhaoxi.tools.reminder_handlers import handle_create_reminder

    with patch("app.db.settings", fresh_db):
        _setup_account("acc-dyn0")
    fresh_db.dynamic_reminder_enabled = False
    ctx = _make_ctx("acc-dyn0")
    with patch("app.db.settings", fresh_db), \
         patch("app.products.zhaoxi.proactive.fulfillment.dynamic_reminder.settings", fresh_db):
        result = handle_create_reminder(
            {"text": "AI 热点", "due_at": "2026-07-15 08:00:00",
             "recur_rule": "weekly:0,2,4", "fulfillment": "dynamic"},
            ctx,
        )
    assert "error" in result


def test_handle_create_reminder_dynamic_allowed_when_enabled(fresh_db):
    """默认全量：总开关开 → 对任意账号放开（不再有账号 allowlist 门控）。"""
    from app.products.zhaoxi.tools.reminder_handlers import handle_create_reminder

    with patch("app.db.settings", fresh_db):
        _setup_account("acc-dyn1")
    fresh_db.dynamic_reminder_enabled = True
    ctx = _make_ctx("acc-dyn1")
    with patch("app.db.settings", fresh_db), \
         patch("app.products.zhaoxi.proactive.fulfillment.dynamic_reminder.settings", fresh_db), \
         patch("app.products.zhaoxi.tools.reminder_handlers.settings", fresh_db):
        result = handle_create_reminder(
            {"text": "AI 热点", "due_at": "2026-07-15 08:00:00",
             "recur_rule": "weekly:0,2,4", "fulfillment": "dynamic"},
            ctx,
        )
    assert result["status"] == "created"
    assert result["fulfillment"] == "dynamic"


def test_handle_create_reminder_dynamic_backend_recomputes_due_at(fresh_db):
    from app.products.zhaoxi.tools.reminder_handlers import handle_create_reminder

    with patch("app.db.settings", fresh_db):
        _setup_account("acc-1")
    fresh_db.dynamic_reminder_enabled = True
    ctx = _make_ctx("acc-1")
    # 模型给了个"错误"的过去日期，但只有时刻(08:00)应被采用，日期由后端按 recur 重算。
    fake_now = datetime(2026, 7, 15, 9, 30, 0)  # 周三，已过 08:00
    with patch("app.db.settings", fresh_db), \
         patch("app.products.zhaoxi.proactive.fulfillment.dynamic_reminder.settings", fresh_db), \
         patch("app.products.zhaoxi.tools.reminder_handlers.settings", fresh_db), \
         patch("app.products.zhaoxi.tools.reminder_handlers.beijing_naive_now", return_value=fake_now):
        result = handle_create_reminder(
            {"text": "AI 热点", "due_at": "2020-01-01 08:00:00",
             "recur_rule": "weekly:0,2,4", "fulfillment": "dynamic", "max_items": 5},
            ctx,
        )
    assert result["status"] == "created"
    assert result["fulfillment"] == "dynamic"
    # 一三五、今天周三已过点 → 下一次周五 08:00。
    assert result["due_at"] == "2026-07-17 08:00:00"


def test_execute_tool_call_blocks_web_search_when_disabled():
    from app.tools.executor import execute_tool_call

    ctx = _make_ctx()
    result = execute_tool_call("web_search", {"query": "OpenClaw"}, ctx)

    assert result["status"] == "failed"
    assert result["error"] == "web_search is disabled"


def test_handle_create_reminder_recurring(fresh_db):
    from app.products.zhaoxi.tools.reminder_handlers import handle_create_reminder
    with patch("app.db.settings", fresh_db):
        _setup_account("acc-tool2")
    ctx = _make_ctx("acc-tool2")
    with patch("app.db.settings", fresh_db):
        result = handle_create_reminder(
            {"text": "给爸妈打电话", "due_at": "2026-06-07 09:00:00", "recur_rule": "weekly:6"},
            ctx,
        )
    assert result["status"] == "created"
    assert result["recur_rule"] == "weekly:6"


def test_handle_create_reminder_invalid_due_at(fresh_db):
    from app.products.zhaoxi.tools.reminder_handlers import handle_create_reminder
    ctx = _make_ctx()
    result = handle_create_reminder({"text": "test", "due_at": "not-a-date"}, ctx)
    assert "error" in result


def test_handle_list_reminders_empty(fresh_db):
    from app.products.zhaoxi.tools.reminder_handlers import handle_list_reminders
    with patch("app.db.settings", fresh_db):
        _setup_account("acc-list")
    ctx = _make_ctx("acc-list")
    with patch("app.db.settings", fresh_db):
        result = handle_list_reminders({}, ctx)
    assert result["reminders"] == []


def test_handle_cancel_reminder(fresh_db):
    from app.db import create_reminder
    from app.products.zhaoxi.tools.reminder_handlers import handle_cancel_reminder
    with patch("app.db.settings", fresh_db):
        _setup_account("acc-cancel")
        r = create_reminder(
            account_id="acc-cancel",
            channel="openclaw-weixin",
            channel_account_id="bot",
            to_user_id="chat-1",
            session_key="sk-cancel",
            text="要取消的提醒",
            due_at="2026-06-01 10:00:00",
        )
    ctx = _make_ctx("acc-cancel")
    with patch("app.db.settings", fresh_db):
        result = handle_cancel_reminder({"reminder_id": r["id"]}, ctx)
    assert result["status"] == "cancelled"


def test_handle_cancel_reminder_wrong_account(fresh_db):
    from app.db import create_reminder
    from app.products.zhaoxi.tools.reminder_handlers import handle_cancel_reminder
    with patch("app.db.settings", fresh_db):
        _setup_account("acc-owner")
        r = create_reminder(
            account_id="acc-owner",
            channel="openclaw-weixin",
            channel_account_id="bot",
            to_user_id="chat-1",
            session_key="sk-o",
            text="别人的提醒",
            due_at="2026-06-01 10:00:00",
        )
    ctx = _make_ctx("acc-intruder")  # different account
    with patch("app.db.settings", fresh_db):
        result = handle_cancel_reminder({"reminder_id": r["id"]}, ctx)
    assert "error" in result


def test_handle_update_reminder(fresh_db):
    from app.db import create_reminder
    from app.products.zhaoxi.tools.reminder_handlers import handle_update_reminder
    with patch("app.db.settings", fresh_db):
        _setup_account("acc-upd")
        r = create_reminder(
            account_id="acc-upd",
            channel="openclaw-weixin",
            channel_account_id="bot",
            to_user_id="chat-1",
            session_key="sk-upd",
            text="原内容",
            due_at="2026-06-01 10:00:00",
        )
    ctx = _make_ctx("acc-upd")
    with patch("app.db.settings", fresh_db):
        result = handle_update_reminder(
            {"reminder_id": r["id"], "text": "新内容", "due_at": "2026-06-02 10:00:00"},
            ctx,
        )
    assert result["status"] == "updated"
    assert result["reminder"]["text"] == "新内容"
