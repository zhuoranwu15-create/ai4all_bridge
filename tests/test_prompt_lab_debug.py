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
    fresh_db.llm_openai_api_key = "openai-key"

    res = client.post(
        "/openclaw/turn",
        json=make_payload("prompt-lab", "m-prompt-lab-1", "first"),
        headers=BRIDGE_HEADERS,
    )
    assert res.status_code == 200

    from app.db import (
        get_debug_trace,
        list_session_messages,
        list_sessions_for_account,
        set_account_onboarding_state,
    )

    session = list_sessions_for_account(account_id=account_id)[0]
    set_account_onboarding_state(account_id=account_id, state="complete")
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
    assert built["prompt_blocks"]["project_context"]["included"] is True
    assert "create_reminder" in built["tooling"]["available_tool_names"]
    assert any(
        item["name"] == "web_search" and item["reason"] == "web_search_disabled"
        for item in built["tooling"]["disabled_tools"]
    )
    assert built["history_metadata"]["count"] >= 1
    assert built["carryover"]["included"] is False

    after_build_messages = list_session_messages(session_id=session["id"], limit=100)
    assert after_build_messages == before_messages

    with patch("app.routers.debug.generate_completion", return_value="edited prompt reply") as mocked_completion:
        res = client.post(
            f"/debug/prompt-lab/accounts/{account_id}/replay",
            json={
                "messages": built["messages"],
                "session_id": session["id"],
                "reason": "test prompt lab replay",
                "provider_id": "chatgpt",
            },
            headers=ADMIN_HEADERS,
        )
    assert res.status_code == 200
    assert mocked_completion.call_args.kwargs["provider"].id == "chatgpt"
    replay = res.json()
    assert replay["status"] == "ok"
    assert replay["reply"] == "edited prompt reply"
    assert replay["llm_provider_id"] == "chatgpt"
    assert replay["llm_model"] == "gpt-4o-mini"
    assert replay["side_effects"] == "llm_only_no_message_no_memory_no_outbound"

    after_replay_messages = list_session_messages(session_id=session["id"], limit=100)
    assert after_replay_messages == before_messages

    trace = get_debug_trace(trace_id=replay["trace_id"])
    assert trace["source"] == "prompt_lab"
    assert trace["llm_model"] == "gpt-4o-mini"
    assert trace["metadata"]["trace_kind"] == "prompt_lab_replay"
    assert trace["metadata"]["llm_provider_id"] == "chatgpt"
    assert trace["reply"] == "edited prompt reply"

    res = client.post(
        f"/debug/prompt-lab/accounts/{account_id}/build",
        json={"source_trace_id": replay["trace_id"]},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200
    loaded = res.json()
    assert loaded["source"] == "trace"
    assert loaded["system_prompt"] == built["system_prompt"]
    assert loaded["messages"] == built["messages"]
    assert loaded["metadata"]["messages_count"] == len(built["messages"])
    assert loaded["metadata"]["system_prompt_chars"] == len(built["system_prompt"])
    assert loaded["prompt_blocks"]["trace_system_prompt"]["chars"] == len(built["system_prompt"])
    assert loaded["history_metadata"]["count"] == len(built["messages"]) - 1


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


def test_prompt_lab_replay_rejects_unconfigured_selected_provider(client, fresh_db):
    account_id = ai4all_account_id("prompt-lab-missing-provider-key")
    fresh_db.admin_debug_plaintext_account_allowlist = account_id

    res = client.post(
        "/openclaw/turn",
        json=make_payload("prompt-lab-missing-provider-key", "m-prompt-lab-missing-key"),
        headers=BRIDGE_HEADERS,
    )
    assert res.status_code == 200

    with patch.dict(
        "os.environ",
        {"LLM_ANTHROPIC_API_KEY": "", "ANTHROPIC_API_KEY": "", "CLAUDE_API_KEY": ""},
    ):
        res = client.post(
            f"/debug/prompt-lab/accounts/{account_id}/replay",
            json={
                "messages": [{"role": "system", "content": "test"}],
                "provider_id": "claude",
                "reason": "test missing provider key",
            },
            headers=ADMIN_HEADERS,
        )

    assert res.status_code == 400
    assert "API key" in res.text


def test_prompt_lab_loads_legacy_trace_with_system_prompt_only(client, fresh_db):
    account_id = ai4all_account_id("prompt-lab-legacy-trace")
    fresh_db.admin_debug_plaintext_account_allowlist = account_id

    res = client.post(
        "/openclaw/turn",
        json=make_payload("prompt-lab-legacy-trace", "m-prompt-lab-legacy", "first"),
        headers=BRIDGE_HEADERS,
    )
    assert res.status_code == 200

    from app.db import insert_debug_trace, list_sessions_for_account

    session = list_sessions_for_account(account_id=account_id)[0]
    trace_id = "legacy-system-prompt-only"
    system_prompt = "legacy system prompt"
    insert_debug_trace(
        trace_id=trace_id,
        account_id=account_id,
        session_id=int(session["id"]),
        message_id=None,
        source="ai4all",
        llm_model="test-model",
        system_prompt=system_prompt,
        messages=[],
        reply="legacy reply",
        metadata={},
    )

    res = client.post(
        f"/debug/prompt-lab/accounts/{account_id}/build",
        json={"source_trace_id": trace_id},
        headers=ADMIN_HEADERS,
    )

    assert res.status_code == 200
    loaded = res.json()
    assert loaded["system_prompt"] == system_prompt
    assert loaded["messages"] == [{"role": "system", "content": system_prompt}]
    assert loaded["metadata"]["messages_count"] == 1
    assert loaded["prompt_blocks"]["trace_system_prompt"]["chars"] == len(system_prompt)


def test_prompt_lab_page_shows_prompt_and_messages_lengths():
    from pathlib import Path

    html = Path("app/static/prompt_debug.html").read_text(encoding="utf-8")

    assert 'id="systemPromptLength"' in html
    assert 'id="messagesJsonLength"' in html
    assert 'id="promptBlocks"' in html
    assert 'id="toolingPanel"' in html
    assert 'id="historyPanel"' in html
    assert 'id="llmProviderSelect"' in html
    assert 'id="llmProviderSwitchBtn"' in html
    assert "function loadLlmProviders()" in html
    assert "function providerLlmStatus(provider)" in html
    assert "未配置 key" in html
    assert "family: provider.family" in html
    assert "/admin/llm/active-family" in html
    assert html.index(
        '<button class="primary" onclick="replayPrompt()">Replay</button>'
    ) < html.index("<h3>LLM Provider</h3>")
    assert html.index("<h3>LLM Provider</h3>") < html.index("<h2>Replay 输出</h2>")
    assert "CONVERSATION_COLLAPSED_LIMIT = 4" in html
    assert "function updateLengthMeters()" in html
    assert "function renderConversation()" in html
    assert "function toggleConversation()" in html
    assert "function renderObservability(data)" in html
