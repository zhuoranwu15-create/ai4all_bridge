from unittest.mock import patch


BRIDGE_HEADERS = {"Authorization": "Bearer test-secret"}


def make_payload(msg_id: str, text: str) -> dict:
    return {
        "account_id": "acc-rem-turn",
        "session_key": "sk-rem-turn",
        "sender_id": "sender-rem-turn",
        "chat_id": "user-rem-turn@im.wechat",
        "chat_type": "private",
        "message_type": "text",
        "message_id": msg_id,
        "text": text,
    }


def test_turn_creates_explicit_reminder_without_llm(client):
    from app.db import list_reminders_for_account

    with patch("app.turn_service.generate_reply", return_value="should not be used") as mock_generate:
        res = client.post(
            "/openclaw/turn",
            json=make_payload("rem-msg-1", "2099-05-22 10:00提醒我检查事情A"),
            headers=BRIDGE_HEADERS,
        )

    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "好的，我会在 2099-05-22 10:00 提醒你：检查事情A" == data["reply"]
    assert data["metadata"]["account_id"] == "sk-rem-turn"
    mock_generate.assert_not_called()

    reminders = list_reminders_for_account(account_id="sk-rem-turn")
    assert len(reminders) == 1
    reminder = reminders[0]
    assert reminder["status"] == "pending"
    assert reminder["due_at"] == "2099-05-22 10:00:00"
    assert reminder["text"] == "检查事情A"
    assert reminder["channel_account_id"] == "acc-rem-turn"
    assert reminder["to_user_id"] == "user-rem-turn@im.wechat"
    assert reminder["session_key"] == "sk-rem-turn"
    assert reminder["metadata"]["parser"] == "explicit_rule_v1"
    assert reminder["metadata"]["source_message_id"] == "rem-msg-1"


def test_turn_does_not_create_ambiguous_reminder(client):
    from app.db import list_reminders_for_account

    with patch("app.turn_service.generate_reply", return_value="mock reply") as mock_generate:
        res = client.post(
            "/openclaw/turn",
            json=make_payload("rem-msg-ambiguous", "下午提醒我去趟派出所"),
            headers=BRIDGE_HEADERS,
        )

    assert res.status_code == 200
    assert res.json()["reply"] == (
        "可以，我现在支持明确时间的一次性提醒。请补充具体日期和时间，比如：今天下午3点提醒我去趟派出所。"
    )
    mock_generate.assert_not_called()
    assert list_reminders_for_account(account_id="sk-rem-turn") == []


def test_turn_duplicate_reminder_message_reuses_reply(client):
    from app.db import list_reminders_for_account

    payload = make_payload("rem-msg-dup", "2099-05-22 10:00提醒我检查事情A")
    first = client.post("/openclaw/turn", json=payload, headers=BRIDGE_HEADERS)
    second = client.post("/openclaw/turn", json=payload, headers=BRIDGE_HEADERS)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert second.json()["reply"] == first.json()["reply"]
    assert len(list_reminders_for_account(account_id="sk-rem-turn")) == 1
