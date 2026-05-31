from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest

from app.turn_context import TurnContext


def _make_ctx(account_id="acc-tool"):
    identity = MagicMock()
    identity.channel = "openclaw-weixin"
    identity.channel_account_id = "bot-1"
    identity.chat_id = "chat-1"
    identity.session_key = "sk-1"
    return TurnContext(
        account_id=account_id,
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
    from app.tools.reminder_handlers import handle_create_reminder
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


def test_execute_tool_call_blocks_web_search_when_disabled():
    from app.tools.executor import execute_tool_call

    ctx = _make_ctx()
    result = execute_tool_call("web_search", {"query": "OpenClaw"}, ctx)

    assert result["status"] == "failed"
    assert result["error"] == "web_search is disabled"


def test_handle_create_reminder_recurring(fresh_db):
    from app.tools.reminder_handlers import handle_create_reminder
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
    from app.tools.reminder_handlers import handle_create_reminder
    ctx = _make_ctx()
    result = handle_create_reminder({"text": "test", "due_at": "not-a-date"}, ctx)
    assert "error" in result


def test_handle_list_reminders_empty(fresh_db):
    from app.tools.reminder_handlers import handle_list_reminders
    with patch("app.db.settings", fresh_db):
        _setup_account("acc-list")
    ctx = _make_ctx("acc-list")
    with patch("app.db.settings", fresh_db):
        result = handle_list_reminders({}, ctx)
    assert result["reminders"] == []


def test_handle_cancel_reminder(fresh_db):
    from app.db import create_reminder
    from app.tools.reminder_handlers import handle_cancel_reminder
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
    from app.tools.reminder_handlers import handle_cancel_reminder
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
    from app.tools.reminder_handlers import handle_update_reminder
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
