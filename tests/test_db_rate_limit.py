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
