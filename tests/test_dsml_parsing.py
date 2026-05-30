"""Tests for DeepSeek DSML tool-call fallback parsing."""
import json
from unittest.mock import MagicMock, patch

from app.llm import _parse_dsml_tool_call


DSML_SAMPLE = (
    "<｜｜DSML｜｜tool_calls>\n"
    "<｜｜DSML｜｜invoke name=\"create_reminder\">\n"
    "<｜｜DSML｜｜parameter name=\"title\" string=\"true\">给父母打电话</｜｜DSML｜｜parameter>\n"
    "<｜｜DSML｜｜parameter name=\"time\" string=\"true\">2026-06-06 20:00:00</｜｜DSML｜｜parameter>\n"
    "<｜｜DSML｜｜parameter name=\"repeat\" string=\"true\">weekly:5</｜｜DSML｜｜parameter>\n"
    "</｜｜DSML｜｜invoke>\n"
    "</｜｜DSML｜｜tool_calls>"
)


def test_parse_dsml_returns_tool_name_and_args():
    result = _parse_dsml_tool_call(DSML_SAMPLE)
    assert result is not None
    tool_name, args = result
    assert tool_name == "create_reminder"
    assert args["title"] == "给父母打电话"
    assert args["time"] == "2026-06-06 20:00:00"
    assert args["repeat"] == "weekly:5"


def test_parse_dsml_returns_none_for_plain_text():
    assert _parse_dsml_tool_call("下午三点已帮你设好提醒 ✅") is None


def test_parse_dsml_returns_none_for_empty():
    assert _parse_dsml_tool_call("") is None


def test_parse_dsml_ascii_pipes():
    """Also matches if the model uses regular ASCII | instead of fullwidth ｜."""
    dsml = (
        "<||DSML||invoke name=\"list_reminders\">"
        "<||DSML||parameter name=\"status\" string=\"true\">pending</||DSML||parameter>"
        "</||DSML||invoke>"
    )
    result = _parse_dsml_tool_call(dsml)
    assert result is not None
    tool_name, args = result
    assert tool_name == "list_reminders"
    assert args["status"] == "pending"


# ─── Alias handling in handle_create_reminder ───────────────────────────────

def _setup_account(account_id: str) -> None:
    from app.db import get_or_create_session
    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="s",
        sender_name=None,
        chat_id="c",
        session_key=f"sk-{account_id}",
    )


def _make_ctx(account_id="dsml-acct-1"):
    ctx = MagicMock()
    ctx.account_id = account_id
    ctx.identity.channel = "openclaw-weixin"
    ctx.identity.channel_account_id = "bot-1"
    ctx.identity.session_key = f"sk-{account_id}"
    ctx.binding.get.return_value = "user@wechat"
    ctx.message_id = "msg-1"
    return ctx


def test_create_reminder_accepts_title_alias(client, fresh_db):
    _setup_account("dsml-acct-1")
    from app.tools.reminder_handlers import handle_create_reminder
    ctx = _make_ctx()
    result = handle_create_reminder(
        {"title": "给父母打电话", "time": "2026-06-06 20:00:00", "repeat": "weekly:5"},
        ctx,
    )
    assert result.get("status") == "created"
    assert "reminder_id" in result


def test_create_reminder_canonical_names_still_work(client, fresh_db):
    _setup_account("dsml-acct-2")
    from app.tools.reminder_handlers import handle_create_reminder
    ctx = _make_ctx("dsml-acct-2")
    result = handle_create_reminder(
        {"text": "喝水", "due_at": "2026-06-06 10:00:00"},
        ctx,
    )
    assert result.get("status") == "created"


def test_create_reminder_missing_text_returns_error(client, fresh_db):
    _setup_account("dsml-acct-3")
    from app.tools.reminder_handlers import handle_create_reminder
    ctx = _make_ctx("dsml-acct-3")
    result = handle_create_reminder({"due_at": "2026-06-06 10:00:00"}, ctx)
    assert "error" in result
