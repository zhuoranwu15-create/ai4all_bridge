import json


ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}


def _configure_multi_family(fresh_db):
    """dev-like config: deepseek(pro+flash) + openai(pro) + anthropic(pro), all with keys."""
    fresh_db.llm_api_key = "deepseek-key"
    fresh_db.llm_openai_api_key = "openai-key"
    fresh_db.llm_anthropic_api_key = "anthropic-key"
    fresh_db.llm_active_family = "deepseek"
    fresh_db.llm_providers_json = ""


def test_admin_llm_lists_family_tier_state(client, fresh_db):
    _configure_multi_family(fresh_db)

    res = client.get("/admin/llm/providers", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["active_family"] == "deepseek"
    assert body["settings_default_family"] == "deepseek"
    assert body["pro_provider"]["id"] == "deepseek-v4-pro"
    assert body["flash_provider"]["id"] == "deepseek"
    assert {provider["id"] for provider in body["providers"]} == {
        "deepseek",
        "deepseek-v4-pro",
        "chatgpt",
        "claude",
    }
    # 每条带 family/tier，且不泄露 api_key
    by_id = {p["id"]: p for p in body["providers"]}
    assert by_id["deepseek"]["family"] == "deepseek" and by_id["deepseek"]["tier"] == "flash"
    assert by_id["chatgpt"]["family"] == "openai" and by_id["chatgpt"]["tier"] == "pro"
    assert all("api_key" not in provider for provider in body["providers"])


def test_admin_llm_switch_active_family(client, fresh_db):
    from app.db import get_llm_runtime_bindings

    _configure_multi_family(fresh_db)

    res = client.patch(
        "/admin/llm/active-family",
        headers=ADMIN_HEADERS,
        json={"family": "openai"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["active_family"] == "openai"
    assert body["pro_provider"]["family"] == "openai"
    assert get_llm_runtime_bindings()["active_family"] == "openai"


def test_admin_llm_switch_unknown_family_404(client, fresh_db):
    _configure_multi_family(fresh_db)
    res = client.patch(
        "/admin/llm/active-family",
        headers=ADMIN_HEADERS,
        json={"family": "nope"},
    )
    assert res.status_code == 404


def test_admin_llm_switch_to_unconfigured_family_rejected(client, fresh_db):
    # openai 家族存在（内置），但没配 key → 切过去会让主对话/后台落到无 key provider，应拒绝。
    fresh_db.llm_api_key = "deepseek-key"
    fresh_db.llm_openai_api_key = ""
    fresh_db.llm_anthropic_api_key = ""
    fresh_db.llm_active_family = "deepseek"
    fresh_db.llm_providers_json = ""

    res = client.patch(
        "/admin/llm/active-family",
        headers=ADMIN_HEADERS,
        json={"family": "openai"},
    )
    assert res.status_code == 400
    assert "API key" in res.text
    # active family 未被改动
    from app.db import get_llm_runtime_bindings

    assert get_llm_runtime_bindings()["active_family"] is None


def test_admin_llm_set_and_clear_tier_override(client, fresh_db):
    from app.db import get_llm_runtime_bindings

    _configure_multi_family(fresh_db)

    # flash 档 override 到跨家族的 claude
    res = client.patch(
        "/admin/llm/tier-override/flash",
        headers=ADMIN_HEADERS,
        json={"provider_id": "claude"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["flash_provider"]["id"] == "claude"
    assert body["flash_override_provider_id"] == "claude"
    assert get_llm_runtime_bindings()["flash_provider_id"] == "claude"

    res = client.delete("/admin/llm/tier-override/flash", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["flash_provider"]["id"] == "deepseek"
    assert body["flash_override_provider_id"] is None
    assert get_llm_runtime_bindings()["flash_provider_id"] is None


def test_admin_llm_tier_override_unknown_tier_404(client, fresh_db):
    _configure_multi_family(fresh_db)
    res = client.patch(
        "/admin/llm/tier-override/turbo",
        headers=ADMIN_HEADERS,
        json={"provider_id": "deepseek"},
    )
    assert res.status_code == 404


def test_admin_llm_tier_override_rejects_unconfigured_provider(client, fresh_db):
    fresh_db.llm_api_key = "deepseek-key"
    fresh_db.llm_active_family = "deepseek"
    fresh_db.llm_openai_api_key = ""  # chatgpt has no key
    fresh_db.llm_providers_json = ""

    res = client.patch(
        "/admin/llm/tier-override/pro",
        headers=ADMIN_HEADERS,
        json={"provider_id": "chatgpt"},
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


def test_admin_llm_page_shows_family_tier_controls():
    from pathlib import Path

    html = Path("app/static/llm.html").read_text(encoding="utf-8")

    assert "Active Family" in html
    assert "/admin/llm/active-family" in html
    assert "/admin/llm/tier-override/" in html
    assert "clearTierOverride" in html
    assert "method: 'DELETE'" in html
