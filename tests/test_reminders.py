from datetime import datetime
from unittest.mock import patch


def _create_account(account_id: str) -> None:
    from app.db import get_or_create_session

    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"session-{account_id}",
    )


def test_reminder_create_due_claim_and_mark_sent(fresh_db):
    from app.db import (
        claim_due_reminder,
        create_outbound_message,
        create_reminder,
        list_due_reminders,
        mark_reminder_sent,
    )

    with patch("app.db.settings", fresh_db):
        _create_account("acc-reminder")
        reminder = create_reminder(
            reminder_id="rem-1",
            account_id="acc-reminder",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-reminder",
            text="提醒内容",
            due_at="2026-05-22 10:00:00",
            metadata={"kind": "manual"},
        )
        due = list_due_reminders(now="2026-05-22 10:00:00")
        claimed = claim_due_reminder(
            reminder_id="rem-1",
            now="2026-05-22 10:00:00",
        )
        duplicate_claim = claim_due_reminder(
            reminder_id="rem-1",
            now="2026-05-22 10:00:00",
        )
        outbound = create_outbound_message(
            account_id="acc-reminder",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-reminder",
            source="reminder",
            text="提醒内容",
            idempotency_key="reminder-rem-1",
            quota_date="2026-05-22",
        )
        sent = mark_reminder_sent(
            reminder_id="rem-1",
            outbound_message_id=outbound["id"],
        )

    assert reminder["status"] == "pending"
    assert reminder["metadata"]["kind"] == "manual"
    assert [item["id"] for item in due] == ["rem-1"]
    assert claimed["status"] == "sending"
    assert claimed["attempts"] == 1
    assert duplicate_claim is None
    assert sent["status"] == "sent"
    assert sent["outbound_message_id"] == outbound["id"]


def test_dispatch_due_reminders_sends_due_one_shot(fresh_db):
    from app.db import create_reminder, get_reminder, list_outbound_messages
    from app.proactive.reminders import dispatch_due_reminders

    fresh_db.proactive_outbound_daily_limit = 3
    now = datetime(2026, 5, 22, 10, 0)
    with (
        patch("app.db.settings", fresh_db),
        patch("app.proactive.messaging.settings", fresh_db),
        patch(
            "app.proactive.messaging.send_weixin_text",
            return_value={"messageId": "openclaw-weixin:rem-1"},
        ) as mock_send,
    ):
        _create_account("acc-dispatch")
        create_reminder(
            reminder_id="rem-dispatch",
            account_id="acc-dispatch",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-dispatch",
            text="记得出门",
            due_at="2026-05-22 09:59:59",
        )
        results = dispatch_due_reminders(now=now)
        reminder = get_reminder(reminder_id="rem-dispatch")
        outbound = list_outbound_messages(account_id="acc-dispatch")

    assert results[0]["status"] == "sent"
    assert reminder["status"] == "sent"
    assert reminder["outbound_message_id"] == outbound[0]["id"]
    assert outbound[0]["status"] == "sent"
    assert outbound[0]["source"] == "reminder"
    assert outbound[0]["idempotency_key"] == "reminder-rem-dispatch"
    assert outbound[0]["metadata"]["reminder_id"] == "rem-dispatch"
    mock_send.assert_called_once_with(
        to_user_id="user@im.wechat",
        text="记得出门",
        gateway_timeout_ms=fresh_db.openclaw_gateway_call_timeout_ms,
        account_id="bot-1",
        idempotency_key="reminder-rem-dispatch",
        session_key="session-acc-dispatch",
        channel="openclaw-weixin",
    )


def test_dispatch_due_reminders_skips_not_due(fresh_db):
    from app.db import create_reminder, get_reminder
    from app.proactive.reminders import dispatch_due_reminders

    now = datetime(2026, 5, 22, 10, 0)
    with (
        patch("app.db.settings", fresh_db),
        patch("app.proactive.messaging.send_weixin_text") as mock_send,
    ):
        _create_account("acc-not-due")
        create_reminder(
            reminder_id="rem-not-due",
            account_id="acc-not-due",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-not-due",
            text="还没到时间",
            due_at="2026-05-22 10:01:00",
        )
        results = dispatch_due_reminders(now=now)
        reminder = get_reminder(reminder_id="rem-not-due")

    assert results == []
    assert reminder["status"] == "pending"
    mock_send.assert_not_called()


def test_dispatch_due_reminder_cancelled_by_quiet_hours(fresh_db):
    from app.db import create_reminder, get_outbound_daily_usage, get_reminder, list_outbound_messages
    from app.proactive.reminders import dispatch_due_reminders

    now = datetime(2026, 5, 22, 23, 0)
    with (
        patch("app.db.settings", fresh_db),
        patch("app.proactive.messaging.settings", fresh_db),
        patch("app.proactive.messaging.send_weixin_text") as mock_send,
    ):
        _create_account("acc-quiet-rem")
        create_reminder(
            reminder_id="rem-quiet",
            account_id="acc-quiet-rem",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-quiet-rem",
            text="夜间提醒",
            due_at="2026-05-22 22:59:59",
        )
        results = dispatch_due_reminders(now=now)
        reminder = get_reminder(reminder_id="rem-quiet")
        outbound = list_outbound_messages(account_id="acc-quiet-rem")
        usage = get_outbound_daily_usage(
            account_id="acc-quiet-rem",
            quota_date="2026-05-22",
        )

    assert results[0]["status"] == "cancelled"
    assert reminder["status"] == "cancelled"
    assert reminder["error"] == "quiet_hours"
    assert outbound[0]["status"] == "cancelled"
    assert outbound[0]["error"] == "quiet_hours"
    assert usage == 0
    mock_send.assert_not_called()


def test_dispatch_due_reminder_marks_gateway_failure(fresh_db):
    from app.db import create_reminder, get_reminder, list_outbound_messages
    from app.proactive.reminders import dispatch_due_reminders

    now = datetime(2026, 5, 22, 10, 0)
    with (
        patch("app.db.settings", fresh_db),
        patch("app.proactive.messaging.settings", fresh_db),
        patch(
            "app.proactive.messaging.send_weixin_text",
            side_effect=RuntimeError("gateway down"),
        ),
    ):
        _create_account("acc-fail-rem")
        create_reminder(
            reminder_id="rem-fail",
            account_id="acc-fail-rem",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-fail-rem",
            text="会失败的提醒",
            due_at="2026-05-22 09:59:59",
        )
        results = dispatch_due_reminders(now=now)
        reminder = get_reminder(reminder_id="rem-fail")
        outbound = list_outbound_messages(account_id="acc-fail-rem")

    assert results[0]["status"] == "failed"
    assert reminder["status"] == "failed"
    assert reminder["error"] == "gateway down"
    assert reminder["outbound_message_id"] == outbound[0]["id"]
    assert outbound[0]["status"] == "failed"
    assert outbound[0]["attempts"] == 1


def test_reminder_recur_columns_exist(fresh_db):
    from app.db import create_reminder, get_reminder
    with patch("app.db.settings", fresh_db):
        from app.db import get_or_create_session
        get_or_create_session(
            account_id="acc-recur",
            channel="openclaw-weixin",
            sender_id="s",
            sender_name=None,
            chat_id="c",
            session_key="sk-recur",
        )
        r = create_reminder(
            account_id="acc-recur",
            channel="openclaw-weixin",
            channel_account_id="bot",
            to_user_id="user",
            session_key="sk-recur",
            text="每周提醒",
            due_at="2026-06-07 09:00:00",
            recur_rule="weekly:5",
        )
        assert r["recur_rule"] == "weekly:5"
        assert r["sent_count"] == 0
        assert r["last_sent_at"] is None
