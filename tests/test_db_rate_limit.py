import pytest
from unittest.mock import patch


def test_get_daily_usage_returns_zero_when_no_record(fresh_db):
    from app.db import get_daily_usage
    with patch("app.db.settings", fresh_db):
        count = get_daily_usage(account_id="acc1", date="2026-01-01")
    assert count == 0


def test_increment_daily_usage_creates_and_increments(fresh_db):
    from app.db import get_or_create_session, increment_daily_usage, get_daily_usage
    with patch("app.db.settings", fresh_db):
        get_or_create_session(
            account_id="acc1", channel="test",
            sender_id="s1", sender_name=None,
            chat_id=None, session_key="sk1",
        )
        count1 = increment_daily_usage(account_id="acc1", date="2026-01-01")
        count2 = increment_daily_usage(account_id="acc1", date="2026-01-01")
        count3 = get_daily_usage(account_id="acc1", date="2026-01-01")
    assert count1 == 1
    assert count2 == 2
    assert count3 == 2


def test_different_dates_tracked_separately(fresh_db):
    from app.db import get_or_create_session, increment_daily_usage, get_daily_usage
    with patch("app.db.settings", fresh_db):
        get_or_create_session(
            account_id="acc1", channel="test",
            sender_id="s1", sender_name=None,
            chat_id=None, session_key="sk1",
        )
        increment_daily_usage(account_id="acc1", date="2026-01-01")
        increment_daily_usage(account_id="acc1", date="2026-01-02")
        count1 = get_daily_usage(account_id="acc1", date="2026-01-01")
        count2 = get_daily_usage(account_id="acc1", date="2026-01-02")
    assert count1 == 1
    assert count2 == 1


def test_get_usage_last_7_days(fresh_db):
    from app.db import get_or_create_session, increment_daily_usage, get_usage_last_7_days
    with patch("app.db.settings", fresh_db):
        get_or_create_session(
            account_id="acc1", channel="test",
            sender_id="s1", sender_name=None,
            chat_id=None, session_key="sk1",
        )
        increment_daily_usage(account_id="acc1", date="2026-01-01")
        increment_daily_usage(account_id="acc1", date="2026-01-01")
        increment_daily_usage(account_id="acc1", date="2026-01-03")
        rows = get_usage_last_7_days(account_id="acc1")
    assert len(rows) == 2
    assert rows[0]["date"] == "2026-01-03"  # ordered DESC
    assert rows[1]["message_count"] == 2


def test_channel_binding_upsert_and_list(fresh_db):
    from app.db import (
        get_or_create_session,
        list_channel_bindings_for_account,
        upsert_channel_binding,
    )

    with patch("app.db.settings", fresh_db):
        get_or_create_session(
            account_id="sk-bind",
            channel="openclaw-weixin",
            sender_id="sender-1",
            sender_name=None,
            chat_id="chat-1",
            session_key="sk-bind",
        )
        first = upsert_channel_binding(
            account_id="sk-bind",
            channel="openclaw-weixin",
            session_key="sk-bind",
            channel_account_id="bot-a",
            sender_id="sender-1",
            chat_id="chat-1",
            raw_identity={"ai4all_account_id": "sk-bind"},
        )
        second = upsert_channel_binding(
            account_id="sk-bind",
            channel="openclaw-weixin",
            session_key="sk-bind",
            channel_account_id="bot-a",
            sender_id="sender-2",
            chat_id="chat-1",
            raw_identity={"ai4all_account_id": "sk-bind", "sender_id": "sender-2"},
        )
        rows = list_channel_bindings_for_account(account_id="sk-bind")

    assert first["id"] == second["id"]
    assert len(rows) == 1
    assert rows[0]["account_id"] == "sk-bind"
    assert rows[0]["channel_account_id"] == "bot-a"
    assert rows[0]["sender_id"] == "sender-2"
    assert rows[0]["raw_identity"]["sender_id"] == "sender-2"
