from app.identity import resolve_openclaw_identity


def test_openclaw_identity_uses_session_key_as_ai4all_account_id():
    identity = resolve_openclaw_identity(
        channel="openclaw-weixin",
        session_key="agent:main:openclaw-weixin:bot-a:direct:user-a",
        channel_account_id="bot-a",
        sender_id="sender-a",
        chat_id="chat-a",
    )

    assert identity.ai4all_account_id == "agent:main:openclaw-weixin:bot-a:direct:user-a"
    assert identity.account_id == identity.ai4all_account_id
    assert identity.channel_account_id == "bot-a"
    assert identity.sender_id == "sender-a"
    assert identity.chat_id == "chat-a"


def test_openclaw_identity_falls_back_to_chat_or_sender_for_mock_payloads():
    identity = resolve_openclaw_identity(
        channel=None,
        session_key=None,
        channel_account_id="bot-a",
        sender_id="sender-a",
        chat_id="chat-a",
    )

    assert identity.ai4all_account_id == "chat-a"
    assert identity.session_key == "chat-a"
    assert identity.sender_id == "sender-a"
    assert identity.channel == "unknown"


def test_openclaw_identity_metadata_keeps_compat_account_id_alias():
    identity = resolve_openclaw_identity(
        channel="openclaw-weixin",
        session_key="sk-a",
        channel_account_id="bot-a",
        sender_id=None,
        chat_id=None,
    )

    assert identity.metadata() == {
        "ai4all_account_id": "sk-a",
        "account_id": "sk-a",
        "session_key": "sk-a",
        "channel": "openclaw-weixin",
        "channel_account_id": "bot-a",
        "sender_id": "sk-a",
        "chat_id": None,
    }
