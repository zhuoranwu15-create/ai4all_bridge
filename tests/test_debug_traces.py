import json
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


def test_debug_trace_records_only_configured_accounts(client, fresh_db):
    fresh_db.debug_trace_account_ids = "sk-acc-debug-a, sk-acc-debug-b"

    with patch("app.turn_service.generate_reply_with_tools", return_value=("debug reply", None)) as mock_generate:
        res = client.post(
            "/openclaw/turn",
            json=make_payload("acc-debug-a", "m-debug-a"),
            headers=BRIDGE_HEADERS,
        )

    assert res.status_code == 200
    data = res.json()
    trace_id = data["metadata"]["debug_trace_id"]
    assert trace_id
    assert mock_generate.call_args.kwargs["system_prompt"]
    assert mock_generate.call_args.kwargs["messages"][0]["content"] == mock_generate.call_args.kwargs["system_prompt"]

    res = client.get(f"/admin/debug/traces/{trace_id}", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    trace = res.json()["trace"]
    assert trace["account_id"] == "sk-acc-debug-a"
    assert trace["source"] == "ai4all"
    assert trace["llm_model"] == "deepseek-v4-flash"
    assert trace["reply_redacted"] is True
    assert trace["reply_chars"] == len("debug reply")
    assert trace["messages_redacted"] is True
    assert trace["system_prompt_redacted"] is True
    assert trace["metadata"]["history_count"] >= 1
    assert trace["metadata"]["block_metrics"]["project_context"]["included"] is True
    assert "create_reminder" in trace["metadata"]["tooling"]["available_tool_names"]
    assert trace["metadata"]["history"]["count"] >= 1
    assert trace["metadata"]["carryover"]["included"] is False
    assert trace["metadata"]["identity"]["ai4all_account_id"] == "sk-acc-debug-a"
    assert trace["metadata"]["identity"]["channel_account_id"] == "acc-debug-a"
    assert trace["metadata"]["agent_context"]["files"]["AGENTS.md"]["exists"] is True
    assert "HEARTBEAT.md" not in trace["metadata"]["agent_context"]["files"]
    assert "system_prompt" not in trace
    assert "messages" not in trace
    assert "reply" not in trace

    res = client.get(f"/admin/plaintext/debug-traces/{trace_id}", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    plaintext_trace = res.json()["trace"]
    assert plaintext_trace["reply"] == "debug reply"
    assert plaintext_trace["messages"][0]["role"] == "system"
    assert plaintext_trace["messages"][0]["content"] == plaintext_trace["system_prompt"]
    assert "### AGENTS.md" in plaintext_trace["system_prompt"]
    assert "### HEARTBEAT.md" not in plaintext_trace["system_prompt"]

    res = client.post(
        "/openclaw/turn",
        json=make_payload("acc-normal", "m-normal"),
        headers=BRIDGE_HEADERS,
    )
    assert res.status_code == 200
    assert res.json()["metadata"]["debug_trace_id"] is None

    res = client.get("/admin/debug/traces", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    traces = res.json()["traces"]
    assert len(traces) == 1
    assert traces[0]["account_id"] == "sk-acc-debug-a"


def test_turn_debug_trace_uses_provider_snapshot_when_active_provider_changes(client, fresh_db):
    account_id = ai4all_account_id("acc-debug-provider")
    fresh_db.debug_trace_account_ids = account_id
    fresh_db.llm_api_key = "deepseek-key"
    fresh_db.llm_openai_api_key = "openai-key"
    fresh_db.llm_providers_json = json.dumps(
        [
            {
                "id": "chatgpt",
                "label": "ChatGPT",
                "protocol": "openai_responses",
                "base_url": "https://api.openai.com",
                "api_key_env": "OPENAI_API_KEY",
                "model": "gpt-snapshot",
            }
        ]
    )
    flash_override = {"value": "chatgpt"}

    def fake_generate(**kwargs):
        assert kwargs["provider"].id == "chatgpt"
        flash_override["value"] = "deepseek"
        return "snapshot reply", None

    def fake_bindings():
        return {
            "active_family": "deepseek",
            "pro_provider_id": None,
            "flash_provider_id": flash_override["value"],
        }

    with patch("app.agent_runtime.llm.service._runtime_bindings", side_effect=fake_bindings):
        with patch("app.turn_service.generate_reply_with_tools", side_effect=fake_generate):
            res = client.post(
                "/openclaw/turn",
                json=make_payload("acc-debug-provider", "m-debug-provider"),
                headers=BRIDGE_HEADERS,
            )

    assert res.status_code == 200
    trace_id = res.json()["metadata"]["debug_trace_id"]
    res = client.get(f"/admin/debug/traces/{trace_id}", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    trace = res.json()["trace"]
    assert trace["llm_model"] == "gpt-snapshot"
    assert trace["metadata"]["llm_provider_id"] == "chatgpt"
    assert trace["metadata"]["llm_protocol"] == "openai_responses"


def test_debug_trace_supports_multiple_accounts(client, fresh_db):
    fresh_db.debug_trace_account_ids = "sk-acc-debug-a,sk-acc-debug-b"

    for account_id in ["acc-debug-a", "acc-debug-b"]:
        res = client.post(
            "/openclaw/turn",
            json=make_payload(account_id, f"m-{account_id}"),
            headers=BRIDGE_HEADERS,
        )
        assert res.status_code == 200
        assert res.json()["metadata"]["debug_trace_id"]

    res = client.get("/admin/debug/traces", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    accounts = {trace["account_id"] for trace in res.json()["traces"]}
    assert accounts == {"sk-acc-debug-a", "sk-acc-debug-b"}


def test_openclaw_debug_trace_ingest(client):
    payload = {
        "trace_id": "openclaw-run-1",
        "account_id": "acc-debug-a",
        "channel": "openclaw-weixin",
        "session_key": "sk-acc-debug-a",
        "message_id": "run-1",
        "source": "openclaw",
        "llm_model": "openclaw-model",
        "system_prompt": "openclaw system",
        "messages": [{"role": "user", "content": "hello from openclaw"}],
        "reply": "native openclaw reply",
        "metadata": {"mode": "path_b_native_run_suppressed"},
        "latency_ms": 123,
    }
    res = client.post("/openclaw/debug-traces", json=payload, headers=BRIDGE_HEADERS)
    assert res.status_code == 200
    assert res.json()["status"] == "ok"

    duplicate = client.post("/openclaw/debug-traces", json=payload, headers=BRIDGE_HEADERS)
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    assert duplicate.json()["metadata"]["account_id"] == "sk-acc-debug-a"
    assert duplicate.json()["metadata"]["channel_account_id"] == "acc-debug-a"

    res = client.get("/admin/debug/traces/openclaw-run-1", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    trace = res.json()["trace"]
    assert trace["account_id"] == "sk-acc-debug-a"
    assert trace["source"] == "openclaw"
    assert trace["llm_model"] == "openclaw-model"
    assert trace["system_prompt_redacted"] is True
    assert trace["reply_redacted"] is True
    assert trace["messages_redacted"] is True
    assert trace["metadata"]["mode"] == "path_b_native_run_suppressed"
    assert trace["metadata"]["identity"]["ai4all_account_id"] == "sk-acc-debug-a"
    assert trace["metadata"]["identity"]["channel_account_id"] == "acc-debug-a"

    res = client.get("/admin/plaintext/debug-traces/openclaw-run-1", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    plaintext_trace = res.json()["trace"]
    assert plaintext_trace["system_prompt"] == "openclaw system"
    assert plaintext_trace["reply"] == "native openclaw reply"
    assert plaintext_trace["messages"][0]["content"] == "hello from openclaw"


def test_openclaw_debug_trace_ingest_accepts_channel_account_id_without_legacy_account_id(client):
    payload = {
        "trace_id": "openclaw-run-channel-account-only",
        "channel_account_id": "acc-debug-channel-only",
        "channel": "openclaw-weixin",
        "session_key": "sk-acc-debug-channel-only",
        "message_id": "run-channel-account-only",
        "source": "openclaw",
        "messages": [{"role": "user", "content": "hello"}],
        "reply": "native reply",
    }

    res = client.post("/openclaw/debug-traces", json=payload, headers=BRIDGE_HEADERS)

    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["metadata"]["account_id"] == "sk-acc-debug-channel-only"
    assert data["metadata"]["channel_account_id"] == "acc-debug-channel-only"

    res = client.get("/admin/debug/traces/openclaw-run-channel-account-only", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    trace = res.json()["trace"]
    assert trace["account_id"] == "sk-acc-debug-channel-only"
    assert trace["metadata"]["identity"]["channel_account_id"] == "acc-debug-channel-only"


def test_failed_assistant_replies_are_excluded_from_llm_history(client):
    with patch("app.turn_service.generate_reply_with_tools", return_value=("healthy reply", None)):
        res = client.post(
            "/openclaw/turn",
            json=make_payload("acc-history", "m1", "first"),
            headers=BRIDGE_HEADERS,
        )
    assert res.status_code == 200

    from app.db import insert_message, list_sessions_for_account

    sessions = list_sessions_for_account(account_id=ai4all_account_id("acc-history"))
    assert len(sessions) == 1
    insert_message(
        account_id=ai4all_account_id("acc-history"),
        session_id=sessions[0]["id"],
        message_id="failed-reply",
        reply_to_message_id="failed-input",
        direction="outbound",
        role="assistant",
        message_type="text",
        content="我这边刚刚有点卡住了，你可以稍后再发我一次。",
        raw={"source": "ai4all"},
        latency_ms=5000,
        error="LLM request failed",
    )

    with patch("app.turn_service.generate_reply_with_tools", return_value=("healthy reply", None)) as mock_generate:
        res = client.post(
            "/openclaw/turn",
            json=make_payload("acc-history", "m2", "second"),
            headers=BRIDGE_HEADERS,
        )
    assert res.status_code == 200
    history = mock_generate.call_args.kwargs["history"]
    assert {"role": "assistant", "content": "healthy reply"} in history
    assert "我这边刚刚有点卡住了" not in "\n".join(item["content"] for item in history)


def test_prompt_preview_includes_agent_context(client):
    res = client.post(
        "/openclaw/turn",
        json=make_payload("acc-preview", "m-preview"),
        headers=BRIDGE_HEADERS,
    )
    assert res.status_code == 200

    res = client.get(f"/debug/accounts/{ai4all_account_id('acc-preview')}/prompt-preview",
                     headers=ADMIN_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["blocks"]["agent_context"]["files"]["IDENTITY.md"]["exists"] is True
    assert data["redacted"] is True
    assert data["prompt_redacted"] is True
    assert data["prompt_chars"] > 0
    assert "prompt" not in data
    assert "HEARTBEAT.md" not in data["blocks"]["agent_context"]["files"]
