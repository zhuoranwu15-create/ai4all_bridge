import asyncio
from datetime import datetime
from unittest.mock import patch


ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}


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


def _create_route(account_id: str) -> None:
    from app.db import upsert_channel_binding

    upsert_channel_binding(
        account_id=account_id,
        channel="openclaw-weixin",
        session_key=f"session-{account_id}",
        channel_account_id="bot-1",
        sender_id="sender",
        chat_id="user@im.wechat",
        raw_identity={"source": "test"},
    )


def test_proactive_scheduler_run_once_calls_due_reminder_dispatch():
    from app.proactive.orchestration.scheduler import ProactiveScheduler

    reminder_calls = []
    commitment_calls = []
    account_calls = []
    reactivation_calls = []
    expired_content_calls = []

    def fake_dispatch_reminders(**kwargs):
        reminder_calls.append(kwargs)
        return [{"status": "sent", "reminder_id": "rem-1"}]

    def fake_scan_account_checks(**kwargs):
        account_calls.append(kwargs)
        return [{"status": "no_op", "account_id": "acc-1"}]

    def fake_dispatch_commitments(**kwargs):
        commitment_calls.append(kwargs)
        return [{"status": "sent", "commitment_id": "com-1"}]

    def fake_dispatch_reactivation(**kwargs):
        reactivation_calls.append(kwargs)
        return [{"action": "would_send", "account_id": "acc-r"}]

    def fake_expire_content_invitations(**kwargs):
        expired_content_calls.append(kwargs)
        return [{"status": "expired", "invitation_id": "ci-expired"}]

    scheduler = ProactiveScheduler(
        interval_seconds=0,
        batch_size=5,
        bypass_quiet_hours=True,
        planning_interval_seconds=1800,
        dispatch_reminders=fake_dispatch_reminders,
        dispatch_commitments=fake_dispatch_commitments,
        dispatch_reactivation=fake_dispatch_reactivation,
        dispatch_icebreakers=lambda **kw: [],
        expire_content_invitations=fake_expire_content_invitations,
        scan_account_checks=fake_scan_account_checks,
    )
    now = datetime(2026, 5, 22, 10, 0)

    result = asyncio.run(scheduler.run_once(now=now))

    assert result["status"] == "ok"
    assert result["reminder_count"] == 1
    assert result["commitment_count"] == 1
    assert result["account_check_count"] == 1
    assert result["reactivation_count"] == 1
    assert result["expired_content_invitation_count"] == 1
    assert reactivation_calls == [{"now": now, "limit": 5, "node_id": None}]
    assert reminder_calls == [
        {
            "now": now,
            "limit": 5,
            "bypass_quiet_hours": True,
            "node_id": None,
        }
    ]
    assert commitment_calls == [
        {
            "now": now,
            "limit": 5,
            "bypass_quiet_hours": True,
            "node_id": None,
        }
    ]
    assert account_calls == [
        {
            "now": now,
            "limit": 5,
            "planning_interval_seconds": 1800,
            "node_id": None,
        }
    ]
    assert expired_content_calls == [
        {
            "now": now,
            "limit": 5,
        }
    ]
    assert scheduler.last_error is None
    assert scheduler.last_run == result


def test_proactive_scheduler_run_once_isolates_failing_step():
    """单个步骤抛错不应饿死后续步骤（尤其排在后面的 reactivation 发送）。"""
    from app.proactive.orchestration.scheduler import ProactiveScheduler

    reactivation_calls = []

    def failing_reminders(**kwargs):
        raise RuntimeError("reminders boom")

    def fake_reactivation(**kwargs):
        reactivation_calls.append(kwargs)
        return [{"action": "sent", "account_id": "acc-r"}]

    scheduler = ProactiveScheduler(
        interval_seconds=0,
        batch_size=5,
        dispatch_reminders=failing_reminders,
        dispatch_commitments=lambda **kw: [],
        dispatch_reactivation=fake_reactivation,
        dispatch_icebreakers=lambda **kw: [],
        expire_content_invitations=lambda **kw: [],
        scan_account_checks=lambda **kw: [],
    )
    now = datetime(2026, 5, 22, 10, 0)

    result = asyncio.run(scheduler.run_once(now=now))

    # reminders 失败被隔离、记录，但 reactivation 仍照常执行
    assert result["status"] == "partial_error"
    assert "reminders" in result["errors"]
    assert "reminders boom" in result["errors"]["reminders"]
    assert result["reminder_count"] == 0
    assert result["reactivation_count"] == 1
    assert len(reactivation_calls) == 1
    assert "reminders" in (scheduler.last_error or "")


def test_admin_proactive_scheduler_status(client):
    res = client.get("/admin/proactive/scheduler", headers=ADMIN_HEADERS)

    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is False
    assert body["scheduler"] is None
    assert body["configured"]["batch_size"] == 20
    assert body["configured"]["planning_interval_seconds"] == 3600


def test_admin_proactive_scheduler_run_once_dispatches_due_reminder(client, fresh_db):
    from app.db import create_reminder, get_reminder, list_outbound_messages

    fresh_db.proactive_outbound_daily_limit = 3
    with (
        patch("app.proactive.delivery.outbound.settings", fresh_db),
        patch(
            "app.proactive.delivery.outbound.send_weixin_text",
            return_value={"messageId": "openclaw-weixin:scheduled-reminder"},
        ) as mock_send,
    ):
        _create_account("acc-scheduler")
        create_reminder(
            reminder_id="rem-scheduler",
            account_id="acc-scheduler",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-scheduler",
            text="自动调度提醒",
            due_at="2000-01-01 00:00:00",
        )

        res = client.post(
            "/admin/proactive/scheduler/run-once?limit=5&bypass_quiet_hours=true",
            headers=ADMIN_HEADERS,
        )
        reminder = get_reminder(reminder_id="rem-scheduler")
        outbound = list_outbound_messages(account_id="acc-scheduler")

    assert res.status_code == 200
    body = res.json()
    assert body["run"]["reminder_count"] == 1
    assert body["run"]["reminders"][0]["status"] == "sent"
    assert reminder["status"] == "sent"
    assert outbound[0]["status"] == "sent"
    assert outbound[0]["idempotency_key"] == "reminder-rem-scheduler-20000101000000"
    mock_send.assert_called_once()


def test_admin_proactive_scheduler_run_once_dispatches_due_commitment(client, fresh_db):
    from app.db import (
        create_proactive_commitment,
        get_proactive_commitment,
        list_outbound_messages,
    )
    from app.proactive.store.account_state import ensure_account_state, get_account_state

    fresh_db.proactive_outbound_daily_limit = 3
    fresh_db.proactive_quiet_hours_start = "00:00"
    fresh_db.proactive_quiet_hours_end = "00:00"
    _create_account("acc-scheduler-commitment")
    _create_route("acc-scheduler-commitment")
    ensure_account_state(
        account_id="acc-scheduler-commitment",
        next_scan_at=datetime(2000, 1, 1, 0, 0),
    )
    create_proactive_commitment(
        commitment_id="com-scheduler",
        account_id="acc-scheduler-commitment",
        text="记得看一下事情 B 的后续。",
        due_at="2000-01-01 00:00:00",
        confidence=0.95,
        reason="测试 commitment",
    )

    with (
        patch("app.proactive.delivery.outbound.settings", fresh_db),
        patch(
            "app.proactive.delivery.outbound.send_weixin_text",
            return_value={"messageId": "openclaw-weixin:scheduled-commitment"},
        ) as mock_send,
    ):
        res = client.post(
            "/admin/proactive/scheduler/run-once?limit=5&bypass_quiet_hours=true",
            headers=ADMIN_HEADERS,
        )
        commitment = get_proactive_commitment(commitment_id="com-scheduler")
        outbound = list_outbound_messages(account_id="acc-scheduler-commitment")
        state = get_account_state(account_id="acc-scheduler-commitment")

    assert res.status_code == 200
    body = res.json()
    assert body["run"]["commitment_count"] == 1
    assert body["run"]["commitments"][0]["status"] == "sent"
    assert commitment["status"] == "sent"
    assert outbound[0]["status"] == "sent"
    assert outbound[0]["source"] == "commitment"
    assert outbound[0]["idempotency_key"] == "commitment-com-scheduler"
    assert outbound[0]["metadata"]["commitment_id"] == "com-scheduler"
    assert state["last_proactive_sent_at"] is not None
    mock_send.assert_called_once()


def test_admin_proactive_scheduler_run_once_scans_due_accounts(client, fresh_db):
    from app.proactive.store.account_state import ensure_account_state, get_account_state

    fresh_db.proactive_quiet_hours_start = "00:00"
    fresh_db.proactive_quiet_hours_end = "00:00"
    _create_account("acc-scan-admin")
    ensure_account_state(
        account_id="acc-scan-admin",
        next_scan_at=datetime(2000, 1, 1, 0, 0),
    )

    with patch("app.proactive.recall.manual_companion.settings", fresh_db):
        res = client.post(
            "/admin/proactive/scheduler/run-once?limit=5",
            headers=ADMIN_HEADERS,
        )
    state = get_account_state(account_id="acc-scan-admin")

    assert res.status_code == 200
    body = res.json()
    assert body["run"]["account_check_count"] == 1
    assert body["run"]["account_checks"][0]["status"] == "skipped"
    assert body["run"]["account_checks"][0]["reason"] == "no_candidate"
    assert state["last_scan_at"] is not None
    assert state["next_scan_at"] > state["last_scan_at"]


def test_admin_proactive_scheduler_run_once_executes_explicit_account_check_candidate(client, fresh_db):
    from app.db import list_outbound_messages
    from app.proactive.store.account_state import ensure_account_state

    fresh_db.proactive_outbound_daily_limit = 3
    fresh_db.proactive_quiet_hours_start = "00:00"
    fresh_db.proactive_quiet_hours_end = "00:00"
    _create_account("acc-scan-send")
    _create_route("acc-scan-send")
    ensure_account_state(
        account_id="acc-scan-send",
        next_scan_at=datetime(2000, 1, 1, 0, 0),
        metadata={
            "account_check_candidate": {
                "id": "admin-candidate",
                "text": "从 scheduler 触发主动检查。",
                "source": "test",
            }
        },
    )

    with (
        patch("app.proactive.recall.manual_companion.settings", fresh_db),
        patch("app.proactive.delivery.outbound.settings", fresh_db),
        patch(
            "app.proactive.delivery.outbound.send_weixin_text",
            return_value={"messageId": "openclaw-weixin:scheduled-account-check"},
        ) as mock_send,
    ):
        res = client.post(
            "/admin/proactive/scheduler/run-once?limit=5",
            headers=ADMIN_HEADERS,
        )
        outbound = list_outbound_messages(account_id="acc-scan-send")

    assert res.status_code == 200
    scan = res.json()["run"]["account_checks"][0]
    assert scan["status"] == "sent"
    assert scan["decision"]["action"] == "send_text"
    assert scan["execution"]["outbound_message"]["status"] == "sent"
    assert outbound[0]["source"] == "account_check"
    assert outbound[0]["status"] == "sent"
    assert outbound[0]["metadata"]["account_check_candidate"]["id"] == "admin-candidate"
    assert scan["account_state"]["last_proactive_sent_at"] is not None
    assert "account_check_candidate" not in scan["account_state"]["metadata"]
    assert scan["account_state"]["metadata"]["account_check_last_sent_candidate"]["id"] == "admin-candidate"
    mock_send.assert_called_once()
