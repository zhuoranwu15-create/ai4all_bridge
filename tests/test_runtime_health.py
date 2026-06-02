from datetime import datetime


def test_health_live_and_ready(client):
    live = client.get("/health/live")
    ready = client.get("/health/ready")

    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "ok"
    assert body["checks"]["db"]["status"] == "ok"
    assert body["checks"]["user_profiles_dir"]["status"] == "ok"
    assert body["checks"]["system_dir"]["status"] == "ok"
    assert body["checks"]["runtime_config"]["status"] == "ok"


def test_scheduler_heartbeat_round_trip(fresh_db):
    from app.db import get_scheduler_heartbeat, record_scheduler_heartbeat

    record_scheduler_heartbeat(
        service="proactive_scheduler",
        status="ok",
        metadata={"interval_seconds": 30},
        seen_at=datetime(2026, 6, 2, 10, 0, 0),
    )

    heartbeat = get_scheduler_heartbeat("proactive_scheduler")

    assert heartbeat is not None
    assert heartbeat["service"] == "proactive_scheduler"
    assert heartbeat["status"] == "ok"
    assert heartbeat["last_seen_at"] == "2026-06-02T10:00:00"
    assert heartbeat["last_success_at"] == "2026-06-02T10:00:00"
    assert heartbeat["last_error_at"] is None
    assert heartbeat["metadata"] == {"interval_seconds": 30}


def test_scheduler_heartbeat_preserves_last_success_on_error(fresh_db):
    from app.db import get_scheduler_heartbeat, record_scheduler_heartbeat

    record_scheduler_heartbeat(
        service="dreaming_scheduler",
        status="ok",
        seen_at=datetime(2026, 6, 2, 10, 0, 0),
    )
    record_scheduler_heartbeat(
        service="dreaming_scheduler",
        status="error",
        error="boom",
        seen_at=datetime(2026, 6, 2, 10, 5, 0),
    )

    heartbeat = get_scheduler_heartbeat("dreaming_scheduler")

    assert heartbeat is not None
    assert heartbeat["status"] == "error"
    assert heartbeat["last_seen_at"] == "2026-06-02T10:05:00"
    assert heartbeat["last_success_at"] == "2026-06-02T10:00:00"
    assert heartbeat["last_error_at"] == "2026-06-02T10:05:00"
    assert heartbeat["last_error"] == "boom"


def test_ops_metrics_counts_recent_message_and_outbound_errors(fresh_db):
    from app.db import (
        create_outbound_message,
        get_ops_metrics,
        get_or_create_session,
        insert_message,
    )

    session_state = get_or_create_session(
        account_id="acc-ops",
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key="ops-session",
    )
    session_id = int(session_state["session"]["id"])
    insert_message(
        account_id="acc-ops",
        session_id=session_id,
        message_id="msg-in",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="hello",
    )
    insert_message(
        account_id="acc-ops",
        session_id=session_id,
        message_id="msg-out",
        reply_to_message_id="msg-in",
        direction="outbound",
        role="assistant",
        message_type="text",
        content="reply",
        latency_ms=123,
        error="llm failed",
    )
    create_outbound_message(
        account_id="acc-ops",
        channel="openclaw-weixin",
        channel_account_id="bot",
        to_user_id="chat",
        session_key="ops-session",
        source="reminder",
        text="提醒",
        idempotency_key="ops-outbound",
        quota_date="2026-06-02",
        status="failed",
        error="send failed",
    )

    metrics = get_ops_metrics(window_minutes=60)

    assert metrics["accounts"]["total"] == 1
    assert metrics["messages"]["inbound_total"] == 1
    assert metrics["messages"]["outbound_total"] == 1
    assert metrics["messages"]["error_total"] == 1
    assert metrics["messages"]["avg_latency_ms"] == 123.0
    assert metrics["outbound_messages"]["failed_total"] == 1
    assert metrics["recent_errors"]["messages"][0]["error"] == "llm failed"
    assert metrics["recent_errors"]["outbound_messages"][0]["error"] == "send failed"


def test_admin_ops_status_endpoint(client):
    res = client.get("/admin/ops/status", headers={"Authorization": "Bearer test-admin"})

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["ready"]["status"] == "ok"
    assert "schedulers" in body
    assert "metrics" in body
    assert body["metrics"]["window_minutes"] == 60


def test_monitor_alert_state_threshold_and_recovery(tmp_path):
    from scripts.monitor_health import (
        _load_state,
        _save_state,
        _should_send_failure_alert,
        _should_send_recovery_alert,
    )

    state_file = tmp_path / "monitor-state.json"
    state = {}

    first = _should_send_failure_alert(
        state=state,
        errors=["ready failed"],
        consecutive_failures=2,
        repeat_after_seconds=3600,
    )
    second = _should_send_failure_alert(
        state=state,
        errors=["ready failed"],
        consecutive_failures=2,
        repeat_after_seconds=3600,
    )
    recovery = _should_send_recovery_alert(state=state, consecutive_failures=2)
    _save_state(str(state_file), state)
    loaded = _load_state(str(state_file))

    assert first is False
    assert second is True
    assert recovery is True
    assert loaded["failure_count"] == 0
    assert loaded["last_status"] == "ok"
