import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _settings(**overrides):
    values = {
        "llm_api_key": "legacy-key",
        "llm_base_url": "https://api.deepseek.com",
        "llm_active_family": "deepseek",
        "llm_task_tiers": "",
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


def _clean_env():
    return patch.dict(
        "os.environ",
        {
            "LLM_OPENAI_API_KEY": "",
            "OPENAI_API_KEY": "",
            "LLM_ANTHROPIC_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
            "CLAUDE_API_KEY": "",
        },
    )


# ---------------------------------------------------------------------------
# family × tier 矩阵
# ---------------------------------------------------------------------------

def test_builtin_matrix_has_family_and_tier_tags():
    from app.agent_runtime.llm.providers import list_llm_providers

    with _clean_env():
        providers = list_llm_providers(_settings())

    by_id = {provider.id: provider for provider in providers}
    assert [provider.id for provider in providers] == [
        "deepseek",
        "deepseek-v4-pro",
        "chatgpt",
        "claude",
    ]
    assert (by_id["deepseek"].family, by_id["deepseek"].tier) == ("deepseek", "flash")
    assert by_id["deepseek"].model == "deepseek-v4-flash"
    assert by_id["deepseek"].api_key == "legacy-key"
    assert (by_id["deepseek-v4-pro"].family, by_id["deepseek-v4-pro"].tier) == ("deepseek", "pro")
    assert by_id["deepseek-v4-pro"].model == "deepseek-v4-pro"
    assert (by_id["chatgpt"].family, by_id["chatgpt"].tier) == ("openai", "pro")
    assert (by_id["claude"].family, by_id["claude"].tier) == ("anthropic", "pro")


def test_builtin_provider_templates_use_provider_specific_keys_and_models():
    from app.agent_runtime.llm.providers import list_llm_providers

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


def test_get_llm_provider_default_returns_active_family_pro():
    from app.agent_runtime.llm.providers import get_llm_provider

    provider = get_llm_provider(None, settings_obj=_settings())

    assert provider.id == "deepseek-v4-pro"
    assert provider.model == "deepseek-v4-pro"
    assert provider.tier == "pro"


def test_get_llm_provider_default_follows_active_family():
    from app.agent_runtime.llm.providers import get_llm_provider

    settings = _settings(llm_active_family="openai", llm_openai_api_key="openai-key")
    provider = get_llm_provider(None, settings_obj=settings)

    assert provider.family == "openai"
    assert provider.tier == "pro"


# ---------------------------------------------------------------------------
# JSON 覆盖 + family/tier 继承
# ---------------------------------------------------------------------------

def test_json_entry_must_declare_family_tier_no_inheritance():
    """JSON 覆盖内置 id 时不做隐式继承：省略 family/tier → 默认 family=""、tier="pro"。"""
    from app.agent_runtime.llm.providers import list_llm_providers

    providers_json = json.dumps(
        [
            {
                "id": "deepseek",
                "label": "DeepSeek V4 Flash",
                "protocol": "openai_chat",
                "base_url": "https://api.deepseek.com",
                "api_key_env": "LLM_API_KEY",
                "model": "deepseek-v4-flash",
            }
        ]
    )
    settings = _settings(llm_providers_json=providers_json)

    by_id = {provider.id: provider for provider in list_llm_providers(settings)}
    # 覆盖后不再继承内置的 (deepseek, flash)，退回默认 tier=pro、family=""
    assert by_id["deepseek"].source == "json"
    assert by_id["deepseek"].family == ""
    assert by_id["deepseek"].tier == "pro"


def test_json_with_explicit_family_tier_is_honored():
    from app.agent_runtime.llm.providers import list_llm_providers

    providers_json = json.dumps(
        [
            {
                "id": "chatgpt",
                "label": "ChatGPT",
                "protocol": "openai_responses",
                "base_url": "https://co.yes.vg",
                "api_key_env": "OPENAI_API_KEY",
                "model": "gpt-5.5",
                "family": "openai",
                "tier": "pro",
            }
        ]
    )
    settings = _settings(llm_providers_json=providers_json, llm_openai_api_key="openai-key")

    providers = {provider.id: provider for provider in list_llm_providers(settings)}
    assert providers["chatgpt"].source == "json"
    assert (providers["chatgpt"].family, providers["chatgpt"].tier) == ("openai", "pro")
    assert providers["chatgpt"].model == "gpt-5.5"


def test_json_can_add_new_family_tier_cell():
    from app.agent_runtime.llm.providers import list_llm_providers, resolve_provider_for_tier

    providers_json = json.dumps(
        [
            {
                "id": "openai-flash",
                "label": "GPT mini",
                "protocol": "openai_responses",
                "base_url": "https://co.yes.vg",
                "api_key_env": "LLM_OPENAI_API_KEY",
                "model": "gpt-5.5-mini",
                "family": "openai",
                "tier": "flash",
            }
        ]
    )
    settings = _settings(llm_providers_json=providers_json, llm_openai_api_key="openai-key")

    by_id = {p.id: p for p in list_llm_providers(settings)}
    assert by_id["openai-flash"].family == "openai"
    assert by_id["openai-flash"].tier == "flash"
    resolved = resolve_provider_for_tier("flash", family="openai", settings_obj=settings)
    assert resolved.id == "openai-flash"


def test_unknown_provider_protocol_is_rejected():
    from app.agent_runtime.llm.providers import list_llm_providers

    settings = _settings(
        llm_providers_json=json.dumps(
            [{"id": "bad", "protocol": "not-real", "base_url": "https://example.com", "model": "m"}]
        )
    )

    with pytest.raises(ValueError, match="unsupported LLM protocol"):
        list_llm_providers(settings)


def test_unknown_tier_is_rejected():
    from app.agent_runtime.llm.providers import list_llm_providers

    settings = _settings(
        llm_providers_json=json.dumps(
            [
                {
                    "id": "bad-tier",
                    "protocol": "openai_chat",
                    "base_url": "https://example.com",
                    "model": "m",
                    "tier": "turbo",
                }
            ]
        )
    )

    with pytest.raises(ValueError, match="unsupported LLM tier"):
        list_llm_providers(settings)


def test_inline_provider_api_key_is_rejected():
    from app.agent_runtime.llm.providers import list_llm_providers

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


# ---------------------------------------------------------------------------
# resolve_provider_for_tier
# ---------------------------------------------------------------------------

def test_resolve_tier_picks_family_pro_and_flash():
    from app.agent_runtime.llm.providers import resolve_provider_for_tier

    settings = _settings()
    assert resolve_provider_for_tier("pro", settings_obj=settings).id == "deepseek-v4-pro"
    assert resolve_provider_for_tier("flash", settings_obj=settings).id == "deepseek"


def test_resolve_tier_override_wins_and_can_cross_family():
    from app.agent_runtime.llm.providers import resolve_provider_for_tier

    settings = _settings(llm_anthropic_api_key="anthropic-key")
    # flash 档 override 到跨家族的 claude(anthropic/pro)
    resolved = resolve_provider_for_tier(
        "flash", family="deepseek", override_provider_id="claude", settings_obj=settings
    )
    assert resolved.id == "claude"


def test_resolve_tier_falls_back_to_family_pro_when_flash_missing():
    from app.agent_runtime.llm.providers import resolve_provider_for_tier

    # openai 家族内置只有 pro，请求 flash 应兜底到 openai pro。
    settings = _settings(llm_active_family="openai", llm_openai_api_key="openai-key")
    resolved = resolve_provider_for_tier("flash", family="openai", settings_obj=settings)
    assert resolved.family == "openai"
    assert resolved.tier == "pro"


def test_resolve_falls_back_to_builtins_when_json_invalid():
    from app.agent_runtime.llm.service import resolve_active_llm_provider

    settings = _settings(llm_providers_json="{not valid json")

    with patch("app.agent_runtime.llm.service.settings", settings), patch("app.agent_runtime.llm.service._runtime_bindings", return_value={}):
        provider = resolve_active_llm_provider()  # 默认 pro

    assert provider.id == "deepseek-v4-pro"
    assert provider.model == "deepseek-v4-pro"


# ---------------------------------------------------------------------------
# tier_for_task
# ---------------------------------------------------------------------------

def test_tier_for_task_defaults():
    from app.agent_runtime.llm.providers import (
        TASK_MAIN_REPLY,
        TASK_MODERATION,
        TASK_ONBOARDING_EXTRACTION,
        tier_for_task,
    )

    settings = _settings()
    assert tier_for_task(TASK_MAIN_REPLY, settings_obj=settings) == "flash"
    assert tier_for_task(TASK_MODERATION, settings_obj=settings) == "flash"
    assert tier_for_task(TASK_ONBOARDING_EXTRACTION, settings_obj=settings) == "flash"


def test_tier_for_task_env_override():
    from app.agent_runtime.llm.providers import (
        TASK_MAIN_REPLY,
        TASK_MODERATION,
        TASK_ONBOARDING_EXTRACTION,
        tier_for_task,
    )

    settings = _settings(llm_task_tiers=json.dumps({"moderation": "pro", "main_reply": "flash"}))
    assert tier_for_task(TASK_MODERATION, settings_obj=settings) == "pro"
    # main_reply 也可被覆盖（.env.example 有文档，turn_service 已按 tier_for_task 解析）
    assert tier_for_task(TASK_MAIN_REPLY, settings_obj=settings) == "flash"
    # 未列出的任务仍走默认 flash
    assert tier_for_task(TASK_ONBOARDING_EXTRACTION, settings_obj=settings) == "flash"


def test_tier_for_task_unknown_task_defaults_flash():
    from app.agent_runtime.llm.providers import tier_for_task

    assert tier_for_task("not_a_real_task", settings_obj=_settings()) == "flash"


def test_tier_for_task_ignores_bad_override_value():
    from app.agent_runtime.llm.providers import TASK_MODERATION, tier_for_task

    settings = _settings(llm_task_tiers=json.dumps({"moderation": "turbo"}))
    assert tier_for_task(TASK_MODERATION, settings_obj=settings) == "flash"
