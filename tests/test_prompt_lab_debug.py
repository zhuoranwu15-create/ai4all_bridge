from unittest.mock import patch


BRIDGE_HEADERS = {"Authorization": "Bearer test-secret"}
ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}


def ai4all_account_id(channel_account_id: str) -> str:
    return f"sk-{channel_account_id}"


def make_payload(account_id: str, msg_id: str, text: str = "hello") -> dict:
    return {
        "account_id": account_id,
        "session_key": f"sk-{account_id}",
        "sender_id": "sender-test",
        "chat_type": "private",
        "message_type": "text",
        "message_id": msg_id,
        "text": text,
    }


def test_prompt_lab_build_and_replay_are_side_effect_free(client, fresh_db):
    account_id = ai4all_account_id("prompt-lab")
    fresh_db.admin_debug_plaintext_account_allowlist = account_id

    res = client.post(
        "/openclaw/turn",
        json=make_payload("prompt-lab", "m-prompt-lab-1", "first"),
        headers=BRIDGE_HEADERS,
    )
    assert res.status_code == 200

    from app.db import get_debug_trace, list_session_messages, list_sessions_for_account

    session = list_sessions_for_account(account_id=account_id)[0]
    before_messages = list_session_messages(session_id=session["id"], limit=100)

    res = client.get(
        f"/debug/prompt-lab/accounts/{account_id}/context-files",
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200
    files = res.json()["files"]
    assert {item["filename"] for item in files} >= {"SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md"}
    assert any("content" in item for item in files)

    res = client.post(
        f"/debug/prompt-lab/accounts/{account_id}/build",
        json={"user_text": "second dry run", "session_id": session["id"]},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200
    built = res.json()
    assert built["messages"][0]["role"] == "system"
    assert "### SOUL.md" in built["system_prompt"]
    assert built["messages"][-1] == {"role": "user", "content": "second dry run"}
    assert built["metadata"]["debug_dry_run"] is True

    after_build_messages = list_session_messages(session_id=session["id"], limit=100)
    assert after_build_messages == before_messages

    with patch("app.main.generate_completion", return_value="edited prompt reply"):
        res = client.post(
            f"/debug/prompt-lab/accounts/{account_id}/replay",
            json={
                "messages": built["messages"],
                "session_id": session["id"],
                "reason": "test prompt lab replay",
            },
            headers=ADMIN_HEADERS,
        )
    assert res.status_code == 200
    replay = res.json()
    assert replay["status"] == "ok"
    assert replay["reply"] == "edited prompt reply"
    assert replay["side_effects"] == "llm_only_no_message_no_memory_no_outbound"

    after_replay_messages = list_session_messages(session_id=session["id"], limit=100)
    assert after_replay_messages == before_messages

    trace = get_debug_trace(trace_id=replay["trace_id"])
    assert trace["source"] == "prompt_lab"
    assert trace["metadata"]["trace_kind"] == "prompt_lab_replay"
    assert trace["reply"] == "edited prompt reply"


def test_prompt_lab_build_redacts_messages_by_default(client):
    account_id = ai4all_account_id("prompt-lab-redacted")
    res = client.post(
        "/openclaw/turn",
        json=make_payload("prompt-lab-redacted", "m-prompt-lab-redacted", "secret text"),
        headers=BRIDGE_HEADERS,
    )
    assert res.status_code == 200

    from app.db import list_sessions_for_account

    session = list_sessions_for_account(account_id=account_id)[0]
    res = client.post(
        f"/debug/prompt-lab/accounts/{account_id}/build",
        json={"user_text": "new secret", "session_id": session["id"]},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["redacted"] is True
    assert data["messages_redacted"] is True
    assert "messages" not in data
    assert "system_prompt" not in data


def test_prompt_lab_page_shows_prompt_and_messages_lengths():
    from pathlib import Path

    html = Path("app/static/prompt_debug.html").read_text(encoding="utf-8")

    assert 'id="systemPromptLength"' in html
    assert 'id="messagesJsonLength"' in html
    assert "function updateLengthMeters()" in html
