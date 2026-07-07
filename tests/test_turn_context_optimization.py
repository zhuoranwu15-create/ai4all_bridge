from unittest.mock import patch


BRIDGE_HEADERS = {"Authorization": "Bearer test-secret"}


def _payload(account_id: str, message_id: str) -> dict:
    return {
        "account_id": account_id,
        "session_key": account_id,
        "sender_id": "sender-context-opt",
        "chat_type": "private",
        "message_type": "text",
        "message_id": message_id,
        "text": "hello",
    }


def test_plain_turn_reads_agent_context_once(client, fresh_db):
    """A completed normal turn should read agent context only during prompt build."""
    from unittest.mock import patch as _patch
    from app.db import get_or_create_session, set_account_onboarding_state
    import app.turn_service as turn_service

    account_id = "context-opt-account"
    with _patch("app.db.settings", fresh_db):
        get_or_create_session(
            account_id=account_id,
            channel="openclaw-weixin",
            sender_id="sender-context-opt",
            sender_name=None,
            chat_id=None,
            session_key=account_id,
        )
        set_account_onboarding_state(account_id=account_id, state="complete")

    with patch("app.turn_service.read_agent_context", wraps=turn_service.read_agent_context) as mock_read:
        resp = client.post(
            "/openclaw/turn",
            headers=BRIDGE_HEADERS,
            json=_payload(account_id, "context-opt-1"),
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert mock_read.call_count == 1
