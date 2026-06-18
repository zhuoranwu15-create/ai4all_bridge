import json


ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}


def test_admin_llm_lists_and_switches_active_provider(client, fresh_db):
    from app.db import get_llm_provider_override_id

    fresh_db.llm_api_key = "deepseek-key"
    fresh_db.llm_openai_api_key = "openai-key"
    fresh_db.llm_providers_json = json.dumps(
        [
            {
                "id": "chatgpt",
                "label": "ChatGPT",
                "protocol": "openai_responses",
                "base_url": "https://co.yes.vg",
                "api_key_env": "OPENAI_API_KEY",
                "model": "gpt-5.5",
            }
        ]
    )

    res = client.get("/admin/llm/providers", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["active_provider_id"] == "deepseek-v4-pro"
    assert body["effective_provider_id"] == "deepseek-v4-pro"
    assert body["settings_default_provider_id"] == "deepseek-v4-pro"
    assert body["runtime_override_provider_id"] is None
    assert {provider["id"] for provider in body["providers"]} == {
        "deepseek",
        "deepseek-v4-pro",
        "chatgpt",
        "claude",
    }
    assert all("api_key" not in provider for provider in body["providers"])

    res = client.patch(
        "/admin/llm/active-provider",
        headers=ADMIN_HEADERS,
        json={"provider_id": "chatgpt"},
    )
    assert res.status_code == 200
    assert res.json()["active_provider_id"] == "chatgpt"
    assert res.json()["runtime_override_provider_id"] == "chatgpt"
    assert get_llm_provider_override_id() == "chatgpt"

    res = client.delete("/admin/llm/active-provider", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    assert res.json()["active_provider_id"] == "deepseek-v4-pro"
    assert res.json()["runtime_override_provider_id"] is None
    assert get_llm_provider_override_id() is None


def test_admin_llm_rejects_unconfigured_provider(client, fresh_db):
    fresh_db.llm_api_key = "deepseek-key"
    fresh_db.llm_providers_json = json.dumps(
        [
            {
                "id": "claude",
                "label": "Claude",
                "protocol": "anthropic_messages",
                "base_url": "https://co.yes.vg",
                "api_key_env": "CLAUDE_API_KEY",
                "model": "claude-sonnet-4-6",
            }
        ]
    )

    res = client.patch(
        "/admin/llm/active-provider",
        headers=ADMIN_HEADERS,
        json={"provider_id": "claude"},
    )
    assert res.status_code == 400
    assert "API key" in res.text


def test_admin_llm_probe_rejects_oversized_message(client):
    res = client.post(
        "/admin/llm/providers/deepseek/probe",
        headers=ADMIN_HEADERS,
        json={"message": "x" * 2001},
    )

    assert res.status_code == 422


def test_admin_llm_page_shows_runtime_override_controls():
    from pathlib import Path

    html = Path("app/static/llm.html").read_text(encoding="utf-8")

    assert "Settings Default" in html
    assert "Runtime Override" in html
    assert 'id="clear-override"' in html
    assert "method: 'DELETE'" in html
