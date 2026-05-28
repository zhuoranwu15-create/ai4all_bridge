from datetime import datetime


def test_business_day_boundary_at_4am():
    from app.session_lifecycle import business_day_for

    assert business_day_for(datetime(2026, 5, 25, 3, 59)) == "2026-05-24"
    assert business_day_for(datetime(2026, 5, 25, 4, 0)) == "2026-05-25"


def test_account_active_session_rotates_when_business_day_changes(fresh_db):
    from app.db import (
        ACCOUNT_ACTIVE_SESSION_KEY,
        get_or_create_account_active_session,
        insert_message,
        list_sessions_for_account,
    )

    first = get_or_create_account_active_session(
        account_id="acc-lifecycle-day",
        channel="openclaw-weixin",
        sender_id="sender-1",
        sender_name=None,
        chat_id="chat-1",
        business_day="2026-05-24",
        max_turns=500,
    )["session"]
    insert_message(
        account_id="acc-lifecycle-day",
        session_id=int(first["id"]),
        message_id="msg-old",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="昨天的上下文",
    )

    second = get_or_create_account_active_session(
        account_id="acc-lifecycle-day",
        channel="openclaw-weixin",
        sender_id="sender-1",
        sender_name=None,
        chat_id="chat-1",
        business_day="2026-05-25",
        max_turns=500,
    )["session"]

    assert second["id"] != first["id"]
    assert second["session_key"] == ACCOUNT_ACTIVE_SESSION_KEY
    assert second["business_day"] == "2026-05-25"
    assert "昨天的上下文" in second["carryover_summary"]

    sessions = list_sessions_for_account(account_id="acc-lifecycle-day", limit=10)
    closed = next(item for item in sessions if item["id"] == first["id"])
    assert closed["status"] == "closed"
    assert closed["close_reason"] == "daily_dreaming"
    assert closed["session_key"] == f"{ACCOUNT_ACTIVE_SESSION_KEY}:{first['id']}"


def test_account_active_session_rotates_when_max_turns_reached(fresh_db):
    from app.db import (
        get_or_create_account_active_session,
        increment_session_turn_count,
    )

    first = get_or_create_account_active_session(
        account_id="acc-lifecycle-max",
        channel="openclaw-weixin",
        sender_id="sender-1",
        sender_name=None,
        chat_id="chat-1",
        business_day="2026-05-25",
        max_turns=1,
    )["session"]
    increment_session_turn_count(session_id=int(first["id"]))

    second = get_or_create_account_active_session(
        account_id="acc-lifecycle-max",
        channel="openclaw-weixin",
        sender_id="sender-1",
        sender_name=None,
        chat_id="chat-1",
        business_day="2026-05-25",
        max_turns=1,
    )["session"]

    assert second["id"] != first["id"]
    assert second["business_day"] == "2026-05-25"
