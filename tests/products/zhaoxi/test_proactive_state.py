from datetime import datetime
from unittest.mock import patch


from tests.factories import create_account as _create_account


from tests.factories import create_route as _create_route


def test_proactive_account_state_due_scan_is_opt_in(fresh_db):
    from app.products.zhaoxi.proactive.store.account_state import (
        ensure_account_state,
        list_due_proactive_account_checks,
    )

    now = datetime(2026, 5, 22, 10, 0)
    _create_account("acc-state")

    assert list_due_proactive_account_checks(now=now) == []

    state = ensure_account_state(
        account_id="acc-state",
        next_scan_at=datetime(2026, 5, 22, 9, 59),
        metadata={"source": "test"},
    )
    ensured_again = ensure_account_state(
        account_id="acc-state",
        next_scan_at=datetime(2026, 5, 23, 9, 59),
        metadata={"source": "should-not-overwrite"},
    )
    due = list_due_proactive_account_checks(now=now)

    assert state["enabled"] is True
    assert state["metadata"]["source"] == "test"
    assert ensured_again["metadata"]["source"] == "test"
    assert ensured_again["next_scan_at"] == "2026-05-22 09:59:00"
    assert [item["account_id"] for item in due] == ["acc-state"]
    assert due[0]["account_status"] == "active"


def test_proactive_account_state_respects_disabled_and_cooldown(fresh_db):
    from app.products.zhaoxi.proactive.store.account_state import (
        ensure_account_state,
        list_due_proactive_account_checks,
        mark_account_proactive_sent,
        set_account_enabled,
    )

    now = datetime(2026, 5, 22, 10, 0)
    _create_account("acc-disabled-state")
    _create_account("acc-cooldown-state")
    _create_account("acc-ready-state")

    ensure_account_state(
        account_id="acc-disabled-state",
        next_scan_at=datetime(2026, 5, 22, 9, 0),
    )
    set_account_enabled(account_id="acc-disabled-state", enabled=False)

    ensure_account_state(
        account_id="acc-cooldown-state",
        next_scan_at=datetime(2026, 5, 22, 9, 0),
    )
    mark_account_proactive_sent(
        account_id="acc-cooldown-state",
        now=now,
        cooldown_until=datetime(2026, 5, 22, 11, 0),
    )

    ensure_account_state(
        account_id="acc-ready-state",
        next_scan_at=datetime(2026, 5, 22, 9, 0),
    )

    due_now = list_due_proactive_account_checks(now=now)
    due_later = list_due_proactive_account_checks(now=datetime(2026, 5, 22, 11, 0))

    assert [item["account_id"] for item in due_now] == ["acc-ready-state"]
    assert {item["account_id"] for item in due_later} == {
        "acc-cooldown-state",
        "acc-ready-state",
    }


def test_mark_account_checked_moves_next_scan_forward(fresh_db):
    from app.products.zhaoxi.proactive.store.account_state import (
        ensure_account_state,
        list_due_proactive_account_checks,
        mark_account_checked,
    )

    now = datetime(2026, 5, 22, 10, 0)
    _create_account("acc-scan-state")
    ensure_account_state(
        account_id="acc-scan-state",
        next_scan_at=datetime(2026, 5, 22, 9, 0),
    )

    state = mark_account_checked(
        account_id="acc-scan-state",
        now=now,
        interval_seconds=30 * 60,
    )

    assert state["last_scan_at"] == "2026-05-22 10:00:00"
    assert state["next_scan_at"] == "2026-05-22 10:30:00"
    assert list_due_proactive_account_checks(now=now) == []
    assert [item["account_id"] for item in list_due_proactive_account_checks(
        now=datetime(2026, 5, 22, 10, 30),
    )] == ["acc-scan-state"]


def test_scan_due_proactive_account_checks_claims_and_marks_no_op(fresh_db):
    from app.products.zhaoxi.proactive.store.account_state import (
        ensure_account_state,
        get_account_state,
        scan_due_proactive_account_checks,
    )

    now = datetime(2026, 5, 22, 10, 0)
    _create_account("acc-account-check-shell")
    _create_route("acc-account-check-shell")
    ensure_account_state(
        account_id="acc-account-check-shell",
        next_scan_at=datetime(2026, 5, 22, 9, 0),
    )

    first = scan_due_proactive_account_checks(
        now=now,
        limit=10,
        planning_interval_seconds=1800,
    )
    second = scan_due_proactive_account_checks(
        now=now,
        limit=10,
        planning_interval_seconds=1800,
    )
    state = get_account_state(account_id="acc-account-check-shell")

    assert first[0]["status"] == "skipped"
    assert first[0]["reason"] == "no_candidate"
    assert first[0]["account_id"] == "acc-account-check-shell"
    assert first[0]["decision"]["action"] == "no_op"
    assert first[0]["execution"]["status"] == "skipped"
    assert second == []
    assert state["last_scan_at"] == "2026-05-22 10:00:00"
    assert state["next_scan_at"] == "2026-05-22 10:30:00"


def test_due_reminder_dispatch_does_not_require_proactive_account_state(fresh_db):
    from app.db import (
        create_reminder,
        get_proactive_account_state,
        get_reminder,
        list_outbound_messages,
    )
    from app.products.zhaoxi.proactive.obligations.reminders import dispatch_due_reminders

    fresh_db.proactive_outbound_daily_limit = 3
    with (
        patch("app.products.zhaoxi.proactive.delivery.outbound.settings", fresh_db),
        patch(
            "app.products.zhaoxi.proactive.delivery.outbound.send_weixin_text",
            return_value={"messageId": "openclaw-weixin:state-regression"},
        ),
    ):
        _create_account("acc-reminder-no-state")
        _create_route("acc-reminder-no-state")
        create_reminder(
            reminder_id="rem-no-state",
            account_id="acc-reminder-no-state",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-reminder-no-state",
            text="不要被 proactive_account_state 过滤掉",
            due_at="2026-05-22 09:59:59",
        )

        assert get_proactive_account_state(account_id="acc-reminder-no-state") is None

        results = dispatch_due_reminders(now=datetime(2026, 5, 22, 10, 0))
        reminder = get_reminder(reminder_id="rem-no-state")
        outbound = list_outbound_messages(account_id="acc-reminder-no-state")

    assert results[0]["status"] == "sent"
    assert reminder["status"] == "sent"
    assert outbound[0]["status"] == "sent"
