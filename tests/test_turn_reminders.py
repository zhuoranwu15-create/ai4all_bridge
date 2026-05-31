"""
End-to-end tests for reminder creation via the /openclaw/turn endpoint
using LLM tool use. The LLM is mocked to return a tool_calls response.
"""
from unittest.mock import patch


def test_turn_creates_reminder_via_tool_call(client, fresh_db):
    """When LLM returns create_reminder tool call, reply is returned correctly."""
    from unittest.mock import patch as _patch
    from app.db import get_or_create_session, set_account_onboarding_state

    # Set up account with completed onboarding so tool path fires
    with _patch("app.db.settings", fresh_db):
        session = get_or_create_session(
            account_id="test-session",
            channel="openclaw-weixin",
            sender_id="user-1",
            sender_name=None,
            chat_id="chat-1",
            session_key="test-session",
        )
        set_account_onboarding_state(account_id="test-session", state="complete")

    tool_call_response = (
        "好的，我会在2026年6月1日上午10点提醒你检查事情A。",
        None,
    )

    with patch("app.turn_service.generate_reply_with_tools", return_value=tool_call_response) as mock_llm:
        resp = client.post(
            "/openclaw/turn",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "session_key": "test-session",
                "channel": "openclaw-weixin",
                "channel_account_id": "bot-1",
                "sender_id": "user-1",
                "chat_id": "chat-1",
                "message_type": "text",
                "text": "明天上午10点提醒我检查事情A",
                "chat_type": "private",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    mock_llm.assert_called_once()
    assert "提醒" in data.get("reply", "")


def test_turn_gates_web_search_tool_registration(client, fresh_db):
    """Main turn only exposes web_search tools when the feature flag is enabled."""
    from unittest.mock import patch as _patch
    from app.db import get_or_create_session, set_account_onboarding_state

    with _patch("app.db.settings", fresh_db):
        get_or_create_session(
            account_id="search-gate-session",
            channel="openclaw-weixin",
            sender_id="user-1",
            sender_name=None,
            chat_id="chat-1",
            session_key="search-gate-session",
        )
        set_account_onboarding_state(account_id="search-gate-session", state="complete")

    def post_turn():
        return client.post(
            "/openclaw/turn",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "session_key": "search-gate-session",
                "channel": "openclaw-weixin",
                "channel_account_id": "bot-1",
                "sender_id": "user-1",
                "chat_id": "chat-1",
                "message_type": "text",
                "text": "查一下今天有什么新闻",
                "chat_type": "private",
            },
        )

    fresh_db.web_search_enabled = False
    with patch("app.turn_service.generate_reply_with_tools", return_value=("mock reply", None)) as mock_llm:
        resp = post_turn()
    assert resp.status_code == 200
    tool_names = {t["function"]["name"] for t in mock_llm.call_args.kwargs["tools"]}
    assert "web_search" not in tool_names

    fresh_db.web_search_enabled = True
    with patch("app.turn_service.generate_reply_with_tools", return_value=("mock reply", None)) as mock_llm:
        resp = post_turn()
    assert resp.status_code == 200
    tool_names = {t["function"]["name"] for t in mock_llm.call_args.kwargs["tools"]}
    assert "web_search" in tool_names


def test_turn_command_bypasses_llm(client, fresh_db):
    """#重置会话 special command does not call generate_reply_with_tools."""
    with patch("app.turn_service.generate_reply_with_tools") as mock_llm:
        resp = client.post(
            "/openclaw/turn",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "session_key": "test-session",
                "channel": "openclaw-weixin",
                "channel_account_id": "bot-1",
                "sender_id": "user-1",
                "chat_id": "chat-1",
                "message_type": "text",
                "text": "#重置会话",
                "chat_type": "private",
            },
        )

    assert resp.status_code == 200
    mock_llm.assert_not_called()
