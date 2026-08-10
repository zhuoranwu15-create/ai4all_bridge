from unittest.mock import patch

import pytest


BRIDGE_HEADERS = {"Authorization": "Bearer test-secret"}


def _create_completed_binding(*, phone: str, channel_account_id: str) -> dict:
    from app.db import (
        create_binding_intent,
        create_or_get_platform_user_by_phone,
        get_binding_intent,
        get_or_create_default_ai4all_account_for_user,
    )
    from app.routers.web import _complete_binding_intent_from_wait_result

    user = create_or_get_platform_user_by_phone(phone=phone)
    account_bundle = get_or_create_default_ai4all_account_for_user(
        app_id="zhaoxi",
        platform_user_id=user["id"],
        display_name="Test Account",
    )
    account = account_bundle["account"]
    intent = create_binding_intent(
        platform_user_id=user["id"],
        account_id=account["id"],
    )
    _complete_binding_intent_from_wait_result(
        get_binding_intent(binding_intent_id=intent["id"]),
        {"connected": True, "accountId": channel_account_id},
    )
    return account


def _turn_payload(
    *,
    channel_account_id: str,
    openclaw_session_key: str,
    message_id: str,
    text: str,
) -> dict:
    return {
        "channel": "openclaw-weixin",
        "channel_account_id": channel_account_id,
        "account_id": channel_account_id,
        "session_key": openclaw_session_key,
        "sender_id": "peer-conv",
        "chat_id": "chat-conv",
        "chat_type": "private",
        "message_type": "text",
        "message_id": message_id,
        "text": text,
        "raw": {"fixture": message_id},
    }


@pytest.mark.slow
def test_account_active_session_ignores_openclaw_session_key_changes(client):
    from app.db import (
        ACCOUNT_ACTIVE_SESSION_KEY,
        get_message_raw,
        list_session_messages,
        list_sessions_for_account,
    )

    account = _create_completed_binding(
        phone="13800009991",
        channel_account_id="route-bot-active-session",
    )

    first = client.post(
        "/openclaw/turn",
        json=_turn_payload(
            channel_account_id="route-bot-active-session",
            openclaw_session_key="openclaw-session-a",
            message_id="conv-msg-1",
            text="第一轮",
        ),
        headers=BRIDGE_HEADERS,
    )
    second = client.post(
        "/openclaw/turn",
        json=_turn_payload(
            channel_account_id="route-bot-active-session",
            openclaw_session_key="openclaw-session-b",
            message_id="conv-msg-2",
            text="第二轮",
        ),
        headers=BRIDGE_HEADERS,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["metadata"]["account_id"] == account["id"]
    assert second.json()["metadata"]["account_id"] == account["id"]

    sessions = list_sessions_for_account(account_id=account["id"])
    assert len(sessions) == 1
    assert sessions[0]["session_key"] == ACCOUNT_ACTIVE_SESSION_KEY

    messages = list_session_messages(session_id=int(sessions[0]["id"]), limit=20)
    assert [m["content"] for m in messages if m["role"] == "user"] == ["第一轮", "第二轮"]

    first_user = next(m for m in messages if m["message_id"] == "conv-msg-1")
    first_raw = get_message_raw(message_db_id=int(first_user["id"]))["raw"]
    assert first_raw["source"] == "openclaw_turn"
    assert first_raw["openclaw_session_key"] == "openclaw-session-a"
    assert first_raw["account_active_session_key"] == ACCOUNT_ACTIVE_SESSION_KEY
    assert first_raw["channel_account_id"] == "route-bot-active-session"
    assert first_raw["raw_payload"] == {"fixture": "conv-msg-1"}

    assistant = next(m for m in messages if m["reply_to_message_id"] == "conv-msg-1")
    assistant_raw = get_message_raw(message_db_id=int(assistant["id"]))["raw"]
    assert assistant_raw["source"] == "ai4all_sync_reply"
    assert assistant_raw["openclaw_session_key"] == "openclaw-session-a"
    assert assistant_raw["account_active_session_key"] == ACCOUNT_ACTIVE_SESSION_KEY


@pytest.mark.slow
def test_normal_chat_does_not_load_daily_notes_into_prompt(client):
    with patch(
        "app.products.zhaoxi.infrastructure.profiles.read_daily_notes",
        side_effect=AssertionError("daily notes should not be loaded for P0 prompt"),
    ) as mock_read_daily_notes, patch(
        "app.turn_service.generate_reply",
        return_value="no daily notes reply",
    ) as mock_generate:
        res = client.post(
            "/openclaw/turn",
            json=_turn_payload(
                channel_account_id="daily-note-bot",
                openclaw_session_key="sk-daily-note-bot",
                message_id="daily-note-msg-1",
                text="普通聊天",
            ),
            headers=BRIDGE_HEADERS,
        )

    assert res.status_code == 200
    mock_read_daily_notes.assert_not_called()
    system_prompt = mock_generate.call_args.kwargs["system_prompt"]
    assert "【今日备注】" not in system_prompt
    assert "daily notes should not be loaded" not in system_prompt

