import pytest

BRIDGE_HEADERS = {"Authorization": "Bearer test-secret"}


def make_payload(msg_id: str, text: str = "hello") -> dict:
    return {
        "account_id": "acc-test",
        "session_key": "sk-test",
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
    assert data.get("no_reply") is not True  # reply should be sent


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
        "/admin/accounts/acc-test/usage",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert res.status_code == 200
    assert res.json()["today"]["message_count"] == 3


def test_usage_endpoint_returns_today_and_history(client):
    # First create the account by posting a turn
    client.post("/openclaw/turn", json=make_payload("m0"), headers=BRIDGE_HEADERS)

    res = client.get(
        "/admin/accounts/acc-test/usage",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "today" in data
    assert "last_7_days" in data
    assert data["today"]["message_count"] == 1


def test_usage_endpoint_404_for_unknown_account(client):
    res = client.get(
        "/admin/accounts/nonexistent/usage",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert res.status_code == 404
