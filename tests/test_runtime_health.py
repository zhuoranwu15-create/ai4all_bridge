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


def test_inbound_rate_counts_messages_and_unique_accounts(fresh_db):
    from app.db import connect, get_inbound_message_rate, get_or_create_session, insert_message

    def add_message(account_id: str, message_id: str, created_at_expr: str) -> None:
        session_state = get_or_create_session(
            account_id=account_id,
            channel="openclaw-weixin",
            sender_id="sender",
            sender_name=None,
            chat_id="chat",
            session_key=f"session-{account_id}",
        )
        row_id = insert_message(
            account_id=account_id,
            session_id=int(session_state["session"]["id"]),
            message_id=message_id,
            reply_to_message_id=None,
            direction="inbound",
            role="user",
            message_type="text",
            content="hello",
        )
        with connect() as conn:
            conn.execute(
                f"UPDATE messages SET created_at = {created_at_expr} WHERE id = ?",
                (row_id,),
            )

    add_message("acc-rate-a", "rate-a-1", "datetime('now', '+8 hours', '-5 minutes')")
    add_message("acc-rate-a", "rate-a-2", "datetime('now', '+8 hours', '-2 minutes')")
    add_message("acc-rate-b", "rate-b-1", "datetime('now', '+8 hours', '-30 minutes')")
    add_message("acc-rate-c", "rate-c-old", "datetime('now', '+8 hours', '-2 hours')")

    rates = {item["minutes"]: item for item in get_inbound_message_rate(windows_minutes=(10, 60))}

    assert rates[10]["count"] == 2
    assert rates[10]["unique_accounts"] == 1
    assert rates[60]["count"] == 3
    assert rates[60]["unique_accounts"] == 2


def test_today_inbound_rate_counts_from_beijing_midnight(fresh_db):
    from app.db import connect, get_or_create_session, get_today_inbound_message_rate, insert_message

    def add_message(
        account_id: str,
        message_id: str,
        *,
        direction: str = "inbound",
        role: str = "user",
        created_at_expr: str = "datetime('now', '+8 hours')",
    ) -> None:
        session_state = get_or_create_session(
            account_id=account_id,
            channel="openclaw-weixin",
            sender_id="sender",
            sender_name=None,
            chat_id="chat",
            session_key=f"session-{account_id}",
        )
        row_id = insert_message(
            account_id=account_id,
            session_id=int(session_state["session"]["id"]),
            message_id=message_id,
            reply_to_message_id=None,
            direction=direction,
            role=role,
            message_type="text",
            content="hello",
        )
        with connect() as conn:
            conn.execute(
                f"UPDATE messages SET created_at = {created_at_expr} WHERE id = ?",
                (row_id,),
            )

    add_message("acc-today-a", "today-a-1")
    add_message("acc-today-a", "today-a-2")
    add_message("acc-today-b", "today-b-1")
    add_message(
        "acc-today-c",
        "today-c-old",
        created_at_expr="datetime('now', '+8 hours', '-1 day')",
    )
    add_message("acc-today-a", "today-outbound", direction="outbound", role="assistant")

    today = get_today_inbound_message_rate()

    assert today["count"] == 3
    assert today["unique_accounts"] == 2
    assert today["since"].endswith(" 00:00:00")


def test_recent_reply_latencies_link_previous_user_message(fresh_db):
    from app.db import connect, get_or_create_session, get_recent_reply_latencies, insert_message

    session_state = get_or_create_session(
        account_id="acc-latency",
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key="latency-session",
    )
    session_id = int(session_state["session"]["id"])

    older_inbound_id = insert_message(
        account_id="acc-latency",
        session_id=session_id,
        message_id="lat-in-1",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="first",
    )
    older_reply_id = insert_message(
        account_id="acc-latency",
        session_id=session_id,
        message_id="lat-out-1",
        reply_to_message_id="lat-in-1",
        direction="outbound",
        role="assistant",
        message_type="text",
        content="reply 1",
        latency_ms=1250,
    )
    newer_inbound_id = insert_message(
        account_id="acc-latency",
        session_id=session_id,
        message_id="lat-in-2",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="second",
    )
    newer_reply_id = insert_message(
        account_id="acc-latency",
        session_id=session_id,
        message_id="lat-out-2",
        reply_to_message_id="lat-in-2",
        direction="outbound",
        role="assistant",
        message_type="text",
        content="reply 2",
    )
    with connect() as conn:
        conn.execute("UPDATE messages SET created_at = '2026-06-18 10:00:00' WHERE id = ?", (older_inbound_id,))
        conn.execute("UPDATE messages SET created_at = '2026-06-18 10:00:02' WHERE id = ?", (older_reply_id,))
        conn.execute("UPDATE messages SET created_at = '2026-06-18 10:01:00' WHERE id = ?", (newer_inbound_id,))
        conn.execute("UPDATE messages SET created_at = '2026-06-18 10:01:03' WHERE id = ?", (newer_reply_id,))

    rows = get_recent_reply_latencies(limit=10)

    assert [row["reply_message_id"] for row in rows[:2]] == ["lat-out-2", "lat-out-1"]
    assert rows[0]["inbound_message_id"] == "lat-in-2"
    assert rows[0]["latency_ms"] == 3000
    assert rows[0]["latency_source"] == "created_at_delta"
    assert rows[1]["inbound_message_id"] == "lat-in-1"
    assert rows[1]["latency_ms"] == 1250
    assert rows[1]["created_at_delta_ms"] == 2000
    assert rows[1]["latency_source"] == "recorded_latency_ms"


def test_account_water_level_counts_distinct_active(fresh_db):
    from app.db import connect, get_account_water_level, upsert_channel_binding

    # 三个账号建绑定（last_seen_at 默认刷成 now）；其中一个手动改成很旧的时间戳。
    with connect() as conn:
        for account_id in ("acc-a", "acc-b", "acc-c"):
            conn.execute("INSERT INTO accounts(id) VALUES (?)", (account_id,))
    for account_id in ("acc-a", "acc-b", "acc-c"):
        upsert_channel_binding(
            account_id=account_id,
            channel="openclaw-weixin",
            session_key=f"sess-{account_id}",
            channel_account_id=None,
            sender_id="sender",
            chat_id="chat",
        )
    # acc-c 标记为很久没活跃 → 应跌出活跃窗口，但仍计入已绑定总数。
    with connect() as conn:
        conn.execute(
            "UPDATE channel_bindings SET last_seen_at = '2000-01-01 00:00:00' WHERE account_id = ?",
            ("acc-c",),
        )

    water_level = get_account_water_level(active_windows_minutes=(15,))

    assert water_level["total_bound_accounts"] == 3
    assert water_level["bound_accounts_by_channel"] == {"openclaw-weixin": 3}
    assert water_level["active_accounts"]["15"] == 2


def test_monitor_record_water_level_appends_snapshot(fresh_db, tmp_path):
    import json

    from app.db import connect, upsert_channel_binding
    from scripts.monitor_health import _record_water_level

    with connect() as conn:
        conn.execute("INSERT INTO accounts(id) VALUES (?)", ("acc-water",))
    upsert_channel_binding(
        account_id="acc-water",
        channel="openclaw-weixin",
        session_key="sess-water",
        channel_account_id=None,
        sender_id="sender",
        chat_id="chat",
    )

    record_file = tmp_path / "water_level.jsonl"
    assert _record_water_level(str(record_file)) is None

    lines = record_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["total_bound_accounts"] == 1
    assert record["bound_accounts_by_channel"] == {"openclaw-weixin": 1}
    assert "ts" in record and record["active_accounts"]["15"] == 1

    # 再采一次 → 追加而非覆盖
    assert _record_water_level(str(record_file)) is None
    assert len(record_file.read_text(encoding="utf-8").splitlines()) == 2


def test_admin_ops_status_endpoint(client):
    res = client.get("/admin/ops/status", headers={"Authorization": "Bearer test-admin"})

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["ready"]["status"] == "ok"
    assert "schedulers" in body
    assert "metrics" in body
    assert body["metrics"]["window_minutes"] == 60

    # 三个调度器的 configured 块必须完整可序列化：曾出现 handler 读取真实 Settings 上
    # 不存在的字段（dreaming interval），而 MagicMock 替身凭空补齐、掩盖成 200 的回归。
    configured = body["schedulers"]["configured"]
    assert configured["dreaming"] == {
        "enabled": False,
        "proactive_process_enabled": True,
        "batch_size": 100,
    }
    assert configured["proactive"]["interval_seconds"] == 30.0
    assert configured["user_meta"]["hour"] == 3


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


def test_monitor_backup_staleness_check(tmp_path):
    from scripts.monitor_health import _check_backup_staleness

    backups = tmp_path / "backups"
    # 没有目录 → 报缺失
    assert "directory missing" in _check_backup_staleness(backups, 93600)

    backups.mkdir()
    # 有目录但没有备份 → 报 no backups
    assert "no backups found" in _check_backup_staleness(backups, 93600)

    # 一个很旧的备份(2020 年) → 报陈旧
    (backups / "ai4all_20200101_000000").mkdir()
    stale = _check_backup_staleness(backups, 93600)
    assert stale is not None and "stale" in stale

    # 加一个当下时间戳的备份 → 不报警(取最新)
    from datetime import datetime

    fresh = datetime.now().strftime("ai4all_%Y%m%d_%H%M%S")
    (backups / fresh).mkdir()
    assert _check_backup_staleness(backups, 93600) is None

    # max_age<=0 关闭检查
    assert _check_backup_staleness(backups, 0) is None


def test_monitor_openclaw_check_passes_for_enabled_channel(monkeypatch):
    from scripts import monitor_health

    calls = []

    def fake_run(command, timeout):
        calls.append(command)
        if command == ["openclaw", "channels", "status", "--probe"]:
            return True, "Gateway reachable."
        if command == ["openclaw", "channels", "list"]:
            return True, "Chat channels:\n- openclaw-weixin default: installed, configured, enabled"
        raise AssertionError(command)

    monkeypatch.setattr(monitor_health, "_run_command", fake_run)

    error = monitor_health._check_openclaw("openclaw-weixin", 5.0)

    assert error is None
    assert calls == [
        ["openclaw", "channels", "status", "--probe"],
        ["openclaw", "channels", "list"],
    ]


def test_monitor_openclaw_check_reports_missing_channel(monkeypatch):
    from scripts import monitor_health

    def fake_run(command, timeout):
        if command == ["openclaw", "channels", "status", "--probe"]:
            return True, "Gateway reachable."
        if command == ["openclaw", "channels", "list"]:
            return True, "Chat channels:\n- telegram default: installed, configured, enabled"
        raise AssertionError(command)

    monkeypatch.setattr(monitor_health, "_run_command", fake_run)

    error = monitor_health._check_openclaw("openclaw-weixin", 5.0)

    assert error == "openclaw channel missing: openclaw-weixin"


def test_monitor_openclaw_check_reports_disabled_channel(monkeypatch):
    from scripts import monitor_health

    def fake_run(command, timeout):
        if command == ["openclaw", "channels", "status", "--probe"]:
            return True, "Gateway reachable."
        if command == ["openclaw", "channels", "list"]:
            return True, "Chat channels:\n- openclaw-weixin default: installed, configured, disabled"
        raise AssertionError(command)

    monkeypatch.setattr(monitor_health, "_run_command", fake_run)

    error = monitor_health._check_openclaw("openclaw-weixin", 5.0)

    assert (
        error
        == "openclaw channel not enabled: - openclaw-weixin default: installed, configured, disabled"
    )


def test_monitor_openclaw_check_reports_probe_failure(monkeypatch):
    from scripts import monitor_health

    def fake_run(command, timeout):
        assert command == ["openclaw", "channels", "status", "--probe"]
        return False, "gateway unreachable"

    monkeypatch.setattr(monitor_health, "_run_command", fake_run)

    error = monitor_health._check_openclaw("openclaw-weixin", 5.0)

    assert error == "openclaw status failed: gateway unreachable"


def test_monitor_wal_check_self_heals_before_alert(monkeypatch):
    from scripts import monitor_health

    monkeypatch.setattr(
        monitor_health,
        "get_database_storage_stats",
        lambda: {"wal_bytes": 200, "journal_mode": "wal", "wal_autocheckpoint_pages": 1000},
    )
    # checkpoint 成功把 -wal 截到阈值以下 → 不告警
    monkeypatch.setattr(
        monitor_health,
        "checkpoint_wal",
        lambda: {"busy": 0, "log_frames": 10, "checkpointed_frames": 10, "wal_bytes_after": 0},
    )

    assert monitor_health._check_wal_size(100) is None


def test_monitor_wal_check_alerts_when_checkpoint_cannot_reclaim(monkeypatch):
    from scripts import monitor_health

    monkeypatch.setattr(
        monitor_health,
        "get_database_storage_stats",
        lambda: {"wal_bytes": 200, "journal_mode": "wal", "wal_autocheckpoint_pages": 1000},
    )
    # 读连接卡住：busy=1，-wal 没缩小 → 仍告警，并带上 checkpoint 诊断
    monkeypatch.setattr(
        monitor_health,
        "checkpoint_wal",
        lambda: {"busy": 1, "log_frames": 50, "checkpointed_frames": 0, "wal_bytes_after": 200},
    )

    error = monitor_health._check_wal_size(100)

    assert error is not None
    assert "too large" in error
    assert "busy=1" in error


def test_monitor_wal_check_skips_checkpoint_when_disabled(monkeypatch):
    from scripts import monitor_health

    monkeypatch.setattr(
        monitor_health,
        "get_database_storage_stats",
        lambda: {"wal_bytes": 200, "journal_mode": "wal", "wal_autocheckpoint_pages": 1000},
    )

    def fail():
        raise AssertionError("checkpoint should not run when disabled")

    monkeypatch.setattr(monitor_health, "checkpoint_wal", fail)

    error = monitor_health._check_wal_size(100, checkpoint_on_bloat=False)

    assert error is not None
    assert "too large" in error
