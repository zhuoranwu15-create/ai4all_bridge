import pytest

from app.schemas import OpenClawTurnRequest

BRIDGE_HEADERS = {"Authorization": "Bearer test-secret"}
AI4ALL_ACCOUNT_ID = "sk-test"
CHANNEL_ACCOUNT_ID = "acc-test"


def make_payload(msg_id: str, text: str = "hello") -> dict:
    return {
        "account_id": CHANNEL_ACCOUNT_ID,
        "session_key": AI4ALL_ACCOUNT_ID,
        "sender_id": "sender-test",
        "chat_type": "private",
        "message_type": "text",
        "message_id": msg_id,
        "text": text,
    }


def test_daily_rate_limit_blocks_after_limit(client):
    # daily limit is 3 (set in conftest test_settings)
    for i in range(3):
        res = client.post("/openclaw/turn", json=make_payload(f"m{i}"), headers=BRIDGE_HEADERS)
        assert res.status_code == 200
        assert res.json()["status"] != "rate_limited"

    res = client.post("/openclaw/turn", json=make_payload("m3"), headers=BRIDGE_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "rate_limited"
    assert data["reply"] == "每日上限"
    assert data["metadata"]["account_id"] == AI4ALL_ACCOUNT_ID
    assert data["metadata"]["ai4all_account_id"] == AI4ALL_ACCOUNT_ID
    assert data["metadata"]["channel_account_id"] == CHANNEL_ACCOUNT_ID
    assert data.get("no_reply") is not True  # reply should be sent


def test_rpm_rate_limit_uses_configured_window(fresh_db, monkeypatch):
    from app.rate_limiter import RateLimiter
    import app.turn_service as turn_service

    fresh_db.rate_limit_daily = 0
    fresh_db.rate_limit_rpm = 2
    # 窗口取足够大（小时级）：本测试验证「同窗口内第 N 次被拦」，而非窗口过期。
    # 走的是完整 handle_openclaw_turn（含 DB / advisory 锁），PG 全量负载下 3 轮调用
    # 偶尔跨越数十秒；若窗口太小，第 1 次命中行会被 DELETE WHERE hit_at < now-window
    # 当过期清掉，导致第 3 次 count 不足而漏拦（与限流逻辑无关的墙钟脆弱性）。
    fresh_db.rate_limit_rpm_window_seconds = 3600

    monkeypatch.setattr(turn_service, "settings", fresh_db)
    monkeypatch.setattr(turn_service, "rate_limiter", RateLimiter())
    monkeypatch.setattr(turn_service, "generate_reply_with_tools", lambda **_: ("mock reply", None))

    for i in range(2):
        res = turn_service.handle_openclaw_turn(OpenClawTurnRequest(**make_payload(f"rpm-{i}")))
        assert res.status != "rate_limited"

    res = turn_service.handle_openclaw_turn(OpenClawTurnRequest(**make_payload("rpm-2")))
    assert res.status == "rate_limited"
    assert res.reply == "每分钟上限"
    assert res.metadata["reason"] == "rpm"


def test_rate_limited_message_not_counted(client):
    # Fill up the 3-message daily limit
    for i in range(3):
        client.post("/openclaw/turn", json=make_payload(f"m{i}"), headers=BRIDGE_HEADERS)

    # Rate limited — should not increment counter further
    for i in range(5):
        res = client.post("/openclaw/turn", json=make_payload(f"extra{i}"), headers=BRIDGE_HEADERS)
        assert res.json()["status"] == "rate_limited"

    # Check usage — still 3, not 8
    res = client.get(
        f"/admin/accounts/{AI4ALL_ACCOUNT_ID}/usage",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert res.status_code == 200
    assert res.json()["today"]["message_count"] == 3


def test_failed_turn_rolls_back_daily_reservation(fresh_db, monkeypatch):
    """D-09 下半刀退款矩阵：正常 turn 确认计入；模型失败 turn 回滚预占、不消耗 daily、不泄漏。"""
    import app.turn_service as turn_service
    from app.db import connect, get_usage_last_7_days

    fresh_db.rate_limit_daily = 5
    fresh_db.rate_limit_rpm = 0  # 关 RPM，避免单测受滑窗干扰
    monkeypatch.setattr(turn_service, "settings", fresh_db)

    def _total() -> int:
        return sum(r["message_count"] for r in get_usage_last_7_days(account_id=AI4ALL_ACCOUNT_ID))

    def _reservation_rows() -> int:
        with connect() as conn:
            return int(conn.execute("SELECT COUNT(*) AS c FROM daily_quota_reservations").fetchone()["c"])

    # 正常 turn：reserve → confirm，计入 1。
    monkeypatch.setattr(turn_service, "generate_reply_with_tools", lambda **_: ("ok reply", None))
    turn_service.handle_openclaw_turn(OpenClawTurnRequest(**make_payload("ok-1")))
    assert _total() == 1
    assert _reservation_rows() == 0  # 已 confirm，无在途

    # 模型失败 turn：generation_error → should_charge False → rollback，计数不增、无残留。
    def _boom(**_):
        raise RuntimeError("model down")

    monkeypatch.setattr(turn_service, "generate_reply_with_tools", _boom)
    res = turn_service.handle_openclaw_turn(OpenClawTurnRequest(**make_payload("fail-2")))
    assert res.status != "rate_limited"  # 正常受理（兜底回复），非限流
    assert _total() == 1  # 仍为 1：失败 turn 不消耗 daily
    assert _reservation_rows() == 0  # 预占已回滚，无泄漏


def test_usage_endpoint_returns_today_and_history(client):
    # First create the account by posting a turn
    client.post("/openclaw/turn", json=make_payload("m0"), headers=BRIDGE_HEADERS)

    res = client.get(
        f"/admin/accounts/{AI4ALL_ACCOUNT_ID}/usage",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "today" in data
    assert "last_7_days" in data
    assert data["today"]["message_count"] == 1


def test_admin_account_includes_channel_bindings(client):
    res = client.post("/openclaw/turn", json=make_payload("m-binding"), headers=BRIDGE_HEADERS)
    assert res.status_code == 200
    binding_id = res.json()["metadata"]["channel_binding_id"]

    res = client.get(
        f"/admin/accounts/{AI4ALL_ACCOUNT_ID}",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert res.status_code == 200
    bindings = res.json()["channel_bindings"]
    assert len(bindings) == 1
    assert bindings[0]["id"] == binding_id
    assert bindings[0]["account_id"] == AI4ALL_ACCOUNT_ID
    assert bindings[0]["session_key"] == AI4ALL_ACCOUNT_ID
    assert bindings[0]["channel_account_id"] == CHANNEL_ACCOUNT_ID
    assert bindings[0]["raw_identity"]["ai4all_account_id"] == AI4ALL_ACCOUNT_ID
    data = res.json()
    assert data["owner_bindings"] == []
    assert data["platform_user"] is None
    assert data["binding_intents"] == []
    assert data["recent_traces"] == []


def test_admin_session_messages_include_trace_id_metadata(client, fresh_db):
    fresh_db.debug_trace_account_ids = AI4ALL_ACCOUNT_ID
    res = client.post("/openclaw/turn", json=make_payload("m-trace-meta"), headers=BRIDGE_HEADERS)
    assert res.status_code == 200
    trace_id = res.json()["metadata"]["debug_trace_id"]
    assert trace_id

    from app.db import list_sessions_for_account

    session = list_sessions_for_account(account_id=AI4ALL_ACCOUNT_ID)[0]
    res = client.get(
        f"/admin/sessions/{session['id']}",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert res.status_code == 200
    messages = res.json()["messages"]
    user_message = next(message for message in messages if message["message_id"] == "m-trace-meta")
    assert user_message["trace_id"] == trace_id

    res = client.get(
        f"/admin/accounts/{AI4ALL_ACCOUNT_ID}",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert res.status_code == 200
    traces = res.json()["recent_traces"]
    assert traces[0]["trace_id"] == trace_id


def test_turn_prefers_channel_account_id_over_legacy_account_id(client):
    payload = make_payload("m-channel-preferred")
    payload["channel_account_id"] = "acc-new"
    payload["account_id"] = "acc-legacy"

    res = client.post("/openclaw/turn", json=payload, headers=BRIDGE_HEADERS)

    assert res.status_code == 200
    data = res.json()
    assert data["metadata"]["account_id"] == AI4ALL_ACCOUNT_ID
    assert data["metadata"]["channel_account_id"] == "acc-new"

    res = client.get(
        f"/admin/accounts/{AI4ALL_ACCOUNT_ID}",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert res.status_code == 200
    bindings = res.json()["channel_bindings"]
    assert len(bindings) == 1
    assert bindings[0]["channel_account_id"] == "acc-new"


def test_usage_endpoint_404_for_unknown_account(client):
    res = client.get(
        "/admin/accounts/nonexistent/usage",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert res.status_code == 404
