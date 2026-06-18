import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _settings(**overrides):
    values = {
        "llm_api_key": "legacy-key",
        "llm_base_url": "https://api.deepseek.com",
        "llm_model": "deepseek-v4-flash",
        "llm_default_provider_id": "deepseek-v4-pro",
        "llm_providers_json": "",
        "llm_openai_base_url": "https://api.openai.com",
        "llm_openai_model": "gpt-4o-mini",
        "llm_openai_api_key": "",
        "llm_anthropic_base_url": "https://api.anthropic.com",
        "llm_anthropic_model": "claude-sonnet-4-6",
        "llm_anthropic_api_key": "",
        "llm_timeout_seconds": 40.0,
        "llm_connect_timeout_seconds": 5.0,
        "llm_max_retries": 1,
        "llm_force_ipv4": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_legacy_provider_defaults_to_deepseek():
    from app.llm_providers import list_llm_providers

    with patch.dict(
        "os.environ",
        {
            "LLM_OPENAI_API_KEY": "",
            "OPENAI_API_KEY": "",
            "LLM_ANTHROPIC_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
            "CLAUDE_API_KEY": "",
        },
    ):
        providers = list_llm_providers(_settings())

    by_id = {provider.id: provider for provider in providers}
    assert [provider.id for provider in providers] == [
        "deepseek",
        "deepseek-v4-pro",
        "chatgpt",
        "claude",
    ]
    assert by_id["deepseek"].protocol == "openai_chat"
    assert by_id["deepseek"].model == "deepseek-v4-flash"
    assert by_id["deepseek"].api_key == "legacy-key"
    assert by_id["deepseek-v4-pro"].protocol == "openai_chat"
    assert by_id["deepseek-v4-pro"].model == "deepseek-v4-pro"
    assert by_id["deepseek-v4-pro"].api_key == "legacy-key"
    assert by_id["deepseek-v4-pro"].api_key_env == "LLM_API_KEY"
    assert by_id["chatgpt"].source == "builtin"
    assert by_id["chatgpt"].api_key == ""
    assert by_id["claude"].source == "builtin"
    assert by_id["claude"].api_key == ""


def test_builtin_provider_templates_use_provider_specific_keys_and_models():
    from app.llm_providers import list_llm_providers

    settings = _settings(
        llm_openai_api_key="openai-key",
        llm_openai_model="gpt-custom",
        llm_anthropic_api_key="anthropic-key",
        llm_anthropic_model="claude-custom",
    )

    providers = {provider.id: provider for provider in list_llm_providers(settings)}

    assert providers["chatgpt"].protocol == "openai_responses"
    assert providers["chatgpt"].model == "gpt-custom"
    assert providers["chatgpt"].api_key == "openai-key"
    assert providers["chatgpt"].api_key_env == "LLM_OPENAI_API_KEY"
    assert providers["claude"].protocol == "anthropic_messages"
    assert providers["claude"].model == "claude-custom"
    assert providers["claude"].api_key == "anthropic-key"
    assert providers["claude"].api_key_env == "LLM_ANTHROPIC_API_KEY"


def test_settings_default_provider_can_select_builtin_model_variant():
    from app.llm_providers import get_llm_provider

    provider = get_llm_provider(None, settings_obj=_settings())

    assert provider.id == "deepseek-v4-pro"
    assert provider.model == "deepseek-v4-pro"


def test_json_providers_resolve_provider_specific_keys():
    from app.llm_providers import list_llm_providers

    providers_json = json.dumps(
        [
            {
                "id": "chatgpt",
                "label": "ChatGPT",
                "protocol": "openai_responses",
                "base_url": "https://co.yes.vg",
                "api_key_env": "OPENAI_API_KEY",
                "model": "gpt-5.5",
            },
            {
                "id": "claude",
                "label": "Claude",
                "protocol": "anthropic_messages",
                "base_url": "https://co.yes.vg",
                "api_key_env": "CLAUDE_API_KEY",
                "model": "claude-sonnet-4-6",
            },
        ]
    )
    settings = _settings(
        llm_providers_json=providers_json,
        llm_openai_api_key="openai-key",
        llm_anthropic_api_key="anthropic-key",
    )

    providers = {provider.id: provider for provider in list_llm_providers(settings)}

    assert providers["chatgpt"].source == "json"
    assert providers["chatgpt"].protocol == "openai_responses"
    assert providers["chatgpt"].api_key == "openai-key"
    assert providers["claude"].source == "json"
    assert providers["claude"].protocol == "anthropic_messages"
    assert providers["claude"].api_key == "anthropic-key"


def test_unknown_provider_protocol_is_rejected():
    from app.llm_providers import list_llm_providers

    settings = _settings(
        llm_providers_json=json.dumps(
            [
                {
                    "id": "bad",
                    "protocol": "not-real",
                    "base_url": "https://example.com",
                    "model": "m",
                }
            ]
        )
    )

    with pytest.raises(ValueError, match="unsupported LLM protocol"):
        list_llm_providers(settings)


def test_inline_provider_api_key_is_rejected():
    from app.llm_providers import list_llm_providers

    settings = _settings(
        llm_providers_json=json.dumps(
            [
                {
                    "id": "bad-key",
                    "protocol": "openai_chat",
                    "base_url": "https://example.com",
                    "model": "m",
                    "api_key": "sk-inline",
                }
            ]
        )
    )

    with pytest.raises(ValueError, match="inline api_key is not allowed"):
        list_llm_providers(settings)


def test_runtime_provider_resolution_falls_back_to_legacy_when_json_is_invalid():
    from app.llm import resolve_active_llm_provider

    settings = _settings(
        llm_api_key="legacy-key",
        llm_providers_json="{not valid json",
    )

    with patch("app.llm.settings", settings), patch("app.llm._stored_provider_override_id", return_value=None):
        provider = resolve_active_llm_provider()

    assert provider.id == "deepseek"
    assert provider.model == "deepseek-v4-flash"
    assert provider.api_key == "legacy-key"
