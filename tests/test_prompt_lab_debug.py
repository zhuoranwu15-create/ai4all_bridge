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
    assert built["execution"]["status"] == "dry_run"
    assert built["execution"]["memory"]["retrieval_performed"] is False

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
    assert loaded["execution"]["reply"] == "edited prompt reply"
    assert loaded["execution"]["latency_ms"] is not None
    assert loaded["execution"]["usage"]["available"] is False


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
    assert loaded["execution"]["reply"] == "legacy reply"
    assert loaded["execution"]["memory"]["legacy_trace"] is True


def test_prompt_lab_trace_aggregates_tool_invocations(client, fresh_db):
    account_id = ai4all_account_id("prompt-lab-tools")
    fresh_db.admin_debug_plaintext_account_allowlist = account_id
    fresh_db.debug_trace_account_ids = account_id

    with patch("app.turn_service.generate_reply_with_tools", return_value=("tool reply", None)):
        res = client.post(
            "/openclaw/turn",
            json=make_payload("prompt-lab-tools", "m-prompt-lab-tools", "set a reminder"),
            headers=BRIDGE_HEADERS,
        )
    assert res.status_code == 200
    trace_id = res.json()["metadata"]["debug_trace_id"]

    from app.db import create_tool_invocation, get_debug_trace

    trace = get_debug_trace(trace_id=trace_id)
    create_tool_invocation(
        account_id=account_id,
        session_id=int(trace["session_id"]),
        message_id="m-prompt-lab-tools",
        tool_call_id="call-1",
        tool_name="create_reminder",
        args={"content": "test"},
        status="succeeded",
        result={"status": "created"},
        latency_ms=23,
        finished=True,
    )

    res = client.post(
        f"/debug/prompt-lab/accounts/{account_id}/build",
        json={"source_trace_id": trace_id},
        headers=ADMIN_HEADERS,
    )

    assert res.status_code == 200
    execution = res.json()["execution"]
    assert execution["reply"] == "tool reply"
    assert execution["tool_invocations"][0]["tool_name"] == "create_reminder"
    assert execution["tool_invocations"][0]["args"] == {"content": "test"}
    assert execution["memory"]["sources"][0]["name"] == "MEMORY.md"
    assert execution["memory"]["retrieval_performed"] is False
    assert "reply_ready_ms" in execution["timings"]


def test_prompt_lab_debug_chat_runs_normal_turn_and_records_trace(client, fresh_db):
    account_id = ai4all_account_id("prompt-lab-chat")
    fresh_db.admin_debug_plaintext_account_allowlist = account_id

    from app.db import (
        get_debug_trace,
        get_or_create_account_active_session,
        list_session_messages,
        set_account_onboarding_state,
    )

    state = get_or_create_account_active_session(
        account_id=account_id,
        channel="debug-chat",
        sender_id="prompt-lab-user",
        sender_name="Prompt Lab",
        chat_id="debug-chat",
    )
    set_account_onboarding_state(account_id=account_id, state="complete")

    with patch("app.turn_service.generate_reply_with_tools", return_value=("debug chat reply", None)):
        res = client.post(
            f"/debug/prompt-lab/accounts/{account_id}/chat",
            json={"text": "hello from debug chat", "session_id": state["session"]["id"]},
            headers=ADMIN_HEADERS,
        )

    assert res.status_code == 200
    data = res.json()
    assert data["turn"]["reply"] == "debug chat reply"
    assert data["trace_id"]
    trace = get_debug_trace(trace_id=data["trace_id"])
    assert trace["account_id"] == account_id
    assert trace["message_id"] == data["message_id"]
    assert trace["reply"] == "debug chat reply"
    messages = list_session_messages(session_id=data["session_id"], limit=20)
    assert [message["content"] for message in messages[-2:]] == [
        "hello from debug chat",
        "debug chat reply",
    ]


def test_prompt_lab_debug_chat_rejects_cross_account_session(client):
    from app.db import get_or_create_account_active_session

    first = get_or_create_account_active_session(
        account_id="debug-chat-account-a",
        channel="debug-chat",
        sender_id="a",
        sender_name=None,
        chat_id="a",
    )
    second = get_or_create_account_active_session(
        account_id="debug-chat-account-b",
        channel="debug-chat",
        sender_id="b",
        sender_name=None,
        chat_id="b",
    )
    assert first["session"]["id"] != second["session"]["id"]

    res = client.post(
        "/debug/prompt-lab/accounts/debug-chat-account-a/chat",
        json={"text": "must not cross accounts", "session_id": second["session"]["id"]},
        headers=ADMIN_HEADERS,
    )

    assert res.status_code == 404


def test_prompt_lab_is_disabled_in_production(client, fresh_db):
    fresh_db.app_env = "production"

    responses = [
        client.get(
            "/debug/prompt-lab/accounts/any/context-files",
            headers=ADMIN_HEADERS,
        ),
        client.get(
            "/debug/prompt-lab/accounts/any/conversation",
            headers=ADMIN_HEADERS,
        ),
        client.post(
            "/debug/prompt-lab/accounts/any/chat",
            json={"text": "blocked"},
            headers=ADMIN_HEADERS,
        ),
        client.post(
            "/debug/prompt-lab/accounts/any/build",
            json={"user_text": "blocked"},
            headers=ADMIN_HEADERS,
        ),
        client.post(
            "/debug/prompt-lab/accounts/any/replay",
            json={"messages": [{"role": "system", "content": "blocked"}]},
            headers=ADMIN_HEADERS,
        ),
        client.get("/ops/prompt_debug.html"),
    ]

    assert [response.status_code for response in responses] == [403] * len(responses)


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
    assert 'id="sessionIdInput"' in html
    assert '<button onclick="loadAccounts()">刷新</button>' in html
    assert 'id="messageFilter"' in html
    assert 'id="executionMeta"' in html
    assert 'id="executedTools"' in html
    assert 'id="memoryPanel"' in html
    assert 'id="latencyPanel"' in html
    assert 'id="chatText"' in html
    assert 'id="sendChatButton"' in html
    assert "function sendDebugChat()" in html
    assert "event.key === 'Enter' && !event.shiftKey" in html
    assert "event.preventDefault()" in html
    assert "function toolChoiceLabel(choice)" in html
    assert "模型自动选择" in html
    assert "可用 · 未调用" in html
    assert "item.message_id === m.reply_to_message_id" in html
    assert ".lab-main { order: 1; }" in html
    assert "prepare_ms: '准备账号与会话'" in html
    assert "reply_generation_ms: '模型生成回复'" in html
    assert "reply_ready_ms: '回复就绪总耗时'" in html
    assert "function loadLlmProviders()" in html
    assert "function providerLlmStatus(provider)" in html
    assert "未配置 key" in html
    assert "provider_id: provider.id" in html
    assert "/admin/llm/active-provider" in html
    assert html.index(
        '<button class="primary" onclick="replayPrompt()">Replay</button>'
    ) < html.index("<h3>LLM Provider</h3>")
    assert html.index("<h3>LLM Provider</h3>") < html.index("<h2>Replay 输出</h2>")
    assert "CONVERSATION_COLLAPSED_LIMIT = 4" in html
    assert "function updateLengthMeters()" in html
    assert "function renderConversation()" in html
    assert "function toggleConversation()" in html
    assert "function renderObservability(data)" in html
