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


def test_multi_account_rpm_shares_subject(fresh_db):
    """D-09：同一真人的两个号 RPM 解析成同一 subject → 共享一套滑窗。

    直接演练 turn_service 的解析逻辑 `get_platform_user_id_for_account(...) or account_id`：
    两号解析到同一 platform_user subject，故经同一 subject 的 check_rpm 命中同一滑窗。
    """
    from app.db import (
        create_ai4all_account_for_user,
        create_or_get_platform_user_by_phone,
        get_platform_user_id_for_account,
    )
    from app.rate_limiter import RateLimiter
    from tests.factories import make_resident_account

    with patch("app.db.settings", fresh_db):
        user = create_or_get_platform_user_by_phone(
            phone="13800040001", display_name="多号RPM"
        )
        # 决策 B：a1 = 用户账号（form-A）；a2 = 居民（form-B，无 binding），经世界归属解析成同一 subject。
        a1 = create_ai4all_account_for_user(
            platform_user_id=user["id"], display_name="甲"
        )["account"]["id"]
        a2 = make_resident_account(user["id"], "乙")

        # turn_service 的 subject 解析（孤儿号回退 account_id）。
        subject_a1 = get_platform_user_id_for_account(account_id=a1) or a1
        subject_a2 = get_platform_user_id_for_account(account_id=a2) or a2
        assert subject_a1 == subject_a2 == user["id"]

        rl = RateLimiter()
        for _ in range(3):
            assert rl.check_rpm(subject_a1, 3) is True
        # 第二个号用同一 subject → 共享滑窗已满 → 被拒。
        assert rl.check_rpm(subject_a2, 3) is False


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
