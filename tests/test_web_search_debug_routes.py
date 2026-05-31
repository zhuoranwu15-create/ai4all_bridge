from app.db import get_or_create_session
from unittest.mock import patch

ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}


def _setup_account(account_id: str) -> None:
    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"sk-{account_id}",
    )


def test_debug_get_web_search_empty(client, fresh_db):
    _setup_account("wsd-empty")

    res = client.get("/debug/web-search/wsd-empty", headers=ADMIN_HEADERS)

    assert res.status_code == 200
    data = res.json()
    assert data["account_id"] == "wsd-empty"
    assert data["tool_schema"]["function"]["name"] == "web_search"
    assert data["tool_invocations"] == []
    assert data["provider_runs"] == []
    assert data["capabilities"]["tool_schema_defined"] is True
    assert data["capabilities"]["currently_in_turn_tools"] is False


def test_debug_simulate_web_search_records_trace(client, fresh_db):
    _setup_account("wsd-sim")

    res = client.post(
        "/debug/web-search/wsd-sim/simulate",
        headers=ADMIN_HEADERS,
        json={
            "query": "OpenClaw web_search",
            "provider": "duckduckgo",
            "status": "succeeded",
            "count": 2,
            "latency_ms": 123,
        },
    )

    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["tool_invocation"]["tool_name"] == "web_search"
    assert data["tool_invocation"]["args"]["query"] == "OpenClaw web_search"
    assert data["provider_run"]["provider"] == "duckduckgo"
    assert data["provider_run"]["status"] == "succeeded"

    listed = client.get("/debug/web-search/wsd-sim", headers=ADMIN_HEADERS).json()
    assert len(listed["tool_invocations"]) == 1
    assert len(listed["provider_runs"]) == 1


def test_debug_simulate_web_search_queued_has_no_provider_run(client, fresh_db):
    _setup_account("wsd-queued")

    res = client.post(
        "/debug/web-search/wsd-queued/simulate",
        headers=ADMIN_HEADERS,
        json={"query": "slow research", "status": "queued"},
    )

    assert res.status_code == 200
    data = res.json()
    assert data["tool_invocation"]["status"] == "queued"
    assert data["tool_invocation"]["result"]["status"] == "queued"
    assert data["provider_run"] is None


def test_debug_web_search_chat_forces_tool_registration(client, fresh_db):
    _setup_account("wsd-chat")

    with patch("app.turn_service.generate_reply_with_tools", return_value=("search reply", None)) as mock_llm:
        res = client.post(
            "/debug/web-search/wsd-chat/chat",
            headers=ADMIN_HEADERS,
            json={"text": "查一下 OpenClaw web_search 最新信息", "provider": "bing"},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["turn"]["reply"] == "search reply"
    assert data["provider_override"] == "bing"
    tool_names = {tool["function"]["name"] for tool in mock_llm.call_args.kwargs["tools"]}
    assert "web_search" in tool_names

    listed = client.get("/debug/web-search/wsd-chat", headers=ADMIN_HEADERS).json()
    messages = listed["conversation"]["messages"]
    assert [message["role"] for message in messages[-2:]] == ["user", "assistant"]
    assert messages[-1]["content"] == "search reply"


def test_debug_run_web_search_can_force_provider(client, fresh_db):
    _setup_account("wsd-run")
    provider_response = {
        "provider": "bing",
        "query": "OpenClaw",
        "results": [
            {
                "title": "OpenClaw result",
                "url": "https://example.com/openclaw",
                "snippet": "result",
                "site_name": "example.com",
                "retrieved_at": "2026-05-31T00:00:00+00:00",
                "score": None,
            }
        ],
        "citations": [{"title": "OpenClaw result", "url": "https://example.com/openclaw"}],
        "retrieved_at": "2026-05-31T00:00:00+00:00",
        "latency_ms": 12,
        "warnings": [],
    }

    with patch("app.tools.web_search_handlers.bing_search", return_value=provider_response) as mock_bing:
        res = client.post(
            "/debug/web-search/wsd-run/run",
            headers=ADMIN_HEADERS,
            json={"query": "OpenClaw", "provider": "bing", "count": 1},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["provider_override"] == "bing"
    assert data["result"]["provider"] == "bing"
    assert data["provider_runs"][0]["provider"] == "bing"
    assert data["provider_runs"][0]["status"] == "succeeded"
    mock_bing.assert_called_once()


def test_debug_run_web_search_rejects_unknown_provider(client, fresh_db):
    _setup_account("wsd-run-invalid")

    res = client.post(
        "/debug/web-search/wsd-run-invalid/run",
        headers=ADMIN_HEADERS,
        json={"query": "OpenClaw", "provider": "tavily", "count": 1},
    )

    assert res.status_code == 400
    assert "unsupported web_search provider" in res.json()["detail"]

    listed = client.get("/debug/web-search/wsd-run-invalid", headers=ADMIN_HEADERS).json()
    assert listed["tool_invocations"] == []
    assert listed["provider_runs"] == []


def test_web_search_debug_static_page_is_local_only(client, fresh_db):
    fresh_db.app_env = "production"
    res = client.get("/ui/web_search_debug.html")
    assert res.status_code == 403

    fresh_db.app_env = "local"
    res = client.get("/ui/web_search_debug.html")
    assert res.status_code == 200
    assert "Web Search Debug Panel" in res.text


def test_debug_web_search_routes_require_admin_auth(client, fresh_db):
    res = client.get("/debug/web-search/wsd-auth")
    assert res.status_code == 401

    res = client.post(
        "/debug/web-search/wsd-auth/simulate",
        json={"query": "hello"},
    )
    assert res.status_code == 401

    res = client.post(
        "/debug/web-search/wsd-auth/chat",
        json={"text": "hello"},
    )
    assert res.status_code == 401
