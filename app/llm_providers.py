"""LLM provider configuration resolution.

The rest of the application talks to app.llm only.  This module keeps provider
selection and protocol/model details out of turn/proactive/dreaming code.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from app.config import settings


SUPPORTED_LLM_PROTOCOLS = {"openai_chat", "openai_responses", "anthropic_messages"}
_DEFAULT_PROVIDER_ID = "deepseek"
_DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
_KEY_FIELD_ALIASES = {
    "LLM_API_KEY": "llm_api_key",
    "DEEPSEEK_API_KEY": "llm_api_key",
    "LLM_OPENAI_API_KEY": "llm_openai_api_key",
    "OPENAI_API_KEY": "llm_openai_api_key",
    "LLM_ANTHROPIC_API_KEY": "llm_anthropic_api_key",
    "ANTHROPIC_API_KEY": "llm_anthropic_api_key",
    "CLAUDE_API_KEY": "llm_anthropic_api_key",
}


@dataclass(frozen=True)
class LLMProviderConfig:
    id: str
    label: str
    protocol: str
    base_url: str
    model: str
    api_key: str
    api_key_env: str = ""
    enabled: bool = True
    supports_tools: bool = True
    timeout_seconds: float = 40.0
    connect_timeout_seconds: float = 5.0
    max_retries: int = 1
    force_ipv4: bool = True
    temperature: float = 0.7
    max_output_tokens: int = 1024
    source: str = "configured"

    def redacted(self) -> Dict[str, Any]:
        """Return a safe admin/debug representation with no secret values."""
        return {
            "id": self.id,
            "label": self.label,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "model": self.model,
            "enabled": self.enabled,
            "supports_tools": self.supports_tools,
            "configured": bool(self.api_key),
            "api_key_env": self.api_key_env,
            "timeout_seconds": self.timeout_seconds,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "max_retries": self.max_retries,
            "force_ipv4": self.force_ipv4,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "source": self.source,
        }


def _safe_get(settings_obj: Any, name: str, default: Any = None) -> Any:
    value = getattr(settings_obj, name, default)
    if type(value).__module__.startswith("unittest.mock"):
        return default
    return value


def _clean_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, (str, int, float, bool)):
        return default
    text = str(value).strip()
    return text if text else default


def _clean_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _clean_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _api_key_from_ref(settings_obj: Any, env_name: str) -> str:
    clean_env = _clean_str(env_name)
    if not clean_env:
        return ""
    field_name = _KEY_FIELD_ALIASES.get(clean_env) or clean_env.lower()
    settings_value = _clean_str(_safe_get(settings_obj, field_name, ""))
    if settings_value:
        return settings_value
    value = os.getenv(clean_env)
    if value:
        return value.strip()
    return ""


def _provider_from_dict(
    raw: Dict[str, Any],
    *,
    settings_obj: Any,
    source: str,
) -> Optional[LLMProviderConfig]:
    provider_id = _clean_str(raw.get("id"))
    model = _clean_str(raw.get("model"))
    if not provider_id or not model:
        return None
    protocol = _clean_str(raw.get("protocol"), "openai_chat")
    if protocol not in SUPPORTED_LLM_PROTOCOLS:
        raise ValueError(f"unsupported LLM protocol for provider {provider_id!r}: {protocol}")
    base_url = _clean_str(raw.get("base_url"))
    if not base_url:
        return None

    if _clean_str(raw.get("api_key")):
        raise ValueError(f"inline api_key is not allowed for LLM provider {provider_id!r}; use api_key_env")
    api_key_env = _clean_str(raw.get("api_key_env"))
    api_key = _api_key_from_ref(settings_obj, api_key_env)
    label = _clean_str(raw.get("label"), provider_id)
    return LLMProviderConfig(
        id=provider_id,
        label=label,
        protocol=protocol,
        base_url=base_url,
        model=model,
        api_key=api_key,
        api_key_env=api_key_env,
        enabled=_clean_bool(raw.get("enabled"), True),
        supports_tools=_clean_bool(raw.get("supports_tools"), True),
        timeout_seconds=max(1.0, _clean_float(raw.get("timeout_seconds"), _clean_float(_safe_get(settings_obj, "llm_timeout_seconds", 40.0), 40.0))),
        connect_timeout_seconds=max(0.1, _clean_float(raw.get("connect_timeout_seconds"), _clean_float(_safe_get(settings_obj, "llm_connect_timeout_seconds", 5.0), 5.0))),
        max_retries=max(0, _clean_int(raw.get("max_retries"), _clean_int(_safe_get(settings_obj, "llm_max_retries", 1), 1))),
        force_ipv4=_clean_bool(raw.get("force_ipv4"), _clean_bool(_safe_get(settings_obj, "llm_force_ipv4", True), True)),
        temperature=_clean_float(raw.get("temperature"), 0.7),
        max_output_tokens=max(1, _clean_int(raw.get("max_output_tokens"), 1024)),
        source=source,
    )


def _legacy_provider(settings_obj: Any) -> LLMProviderConfig:
    provider_id = _clean_str(_safe_get(settings_obj, "llm_default_provider_id", ""), _DEFAULT_PROVIDER_ID)
    base_url = _clean_str(_safe_get(settings_obj, "llm_base_url", ""), "https://api.deepseek.com")
    model = _clean_str(_safe_get(settings_obj, "llm_model", ""), _DEFAULT_DEEPSEEK_MODEL)
    label = (
        "DeepSeek V4 Flash"
        if provider_id == _DEFAULT_PROVIDER_ID and model == _DEFAULT_DEEPSEEK_MODEL
        else "DeepSeek"
        if "deepseek" in (base_url + " " + model).lower()
        else "Default LLM"
    )
    return LLMProviderConfig(
        id=provider_id,
        label=label,
        protocol="openai_chat",
        base_url=base_url,
        model=model,
        api_key=_clean_str(_safe_get(settings_obj, "llm_api_key", "")),
        api_key_env="LLM_API_KEY",
        enabled=True,
        supports_tools=True,
        timeout_seconds=max(1.0, _clean_float(_safe_get(settings_obj, "llm_timeout_seconds", 40.0), 40.0)),
        connect_timeout_seconds=max(0.1, _clean_float(_safe_get(settings_obj, "llm_connect_timeout_seconds", 5.0), 5.0)),
        max_retries=max(0, _clean_int(_safe_get(settings_obj, "llm_max_retries", 1), 1)),
        force_ipv4=_clean_bool(_safe_get(settings_obj, "llm_force_ipv4", True), True),
        source="legacy",
    )


def _model_provider_item(
    *,
    provider_id: str,
    label: str,
    protocol: str,
    base_url: str,
    api_key_env: str,
    model: str,
    supports_tools: bool = True,
) -> Dict[str, Any]:
    """Create a provider entry for one selectable model variant."""
    return {
        "id": provider_id,
        "label": label,
        "protocol": protocol,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "model": model,
        "supports_tools": supports_tools,
    }


def _builtin_provider_items(settings_obj: Any) -> Iterable[Dict[str, Any]]:
    """Return common provider templates exposed in admin even before JSON overrides."""
    deepseek_base_url = _clean_str(
        _safe_get(settings_obj, "llm_base_url", ""),
        "https://api.deepseek.com",
    )
    openai_base_url = _clean_str(
        _safe_get(settings_obj, "llm_openai_base_url", ""),
        "https://api.openai.com",
    )
    anthropic_base_url = _clean_str(
        _safe_get(settings_obj, "llm_anthropic_base_url", ""),
        "https://api.anthropic.com",
    )
    return [
        _model_provider_item(
            provider_id="deepseek-v4-pro",
            label="DeepSeek V4 Pro",
            protocol="openai_chat",
            base_url=deepseek_base_url,
            api_key_env="LLM_API_KEY",
            model="deepseek-v4-pro",
        ),
        _model_provider_item(
            provider_id="chatgpt",
            label="ChatGPT",
            protocol="openai_responses",
            base_url=openai_base_url,
            api_key_env="LLM_OPENAI_API_KEY",
            model=_clean_str(
                _safe_get(settings_obj, "llm_openai_model", ""),
                "gpt-4o-mini",
            ),
        ),
        _model_provider_item(
            provider_id="claude",
            label="Claude",
            protocol="anthropic_messages",
            base_url=anthropic_base_url,
            api_key_env="LLM_ANTHROPIC_API_KEY",
            model=_clean_str(
                _safe_get(settings_obj, "llm_anthropic_model", ""),
                "claude-sonnet-4-6",
            ),
        ),
    ]


def get_legacy_llm_provider(settings_obj: Any = settings) -> LLMProviderConfig:
    """Return the legacy single-provider config without parsing LLM_PROVIDERS_JSON."""
    return _legacy_provider(settings_obj)


def _raw_provider_items(settings_obj: Any) -> Iterable[Dict[str, Any]]:
    raw = _clean_str(_safe_get(settings_obj, "llm_providers_json", ""))
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ValueError("LLM_PROVIDERS_JSON must be valid JSON") from err
    if isinstance(data, dict):
        data = data.get("providers", [])
    if not isinstance(data, list):
        raise ValueError("LLM_PROVIDERS_JSON must be a list or an object with providers")
    out: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("each LLM provider config must be an object")
        out.append(item)
    return out


def list_llm_providers(settings_obj: Any = settings) -> List[LLMProviderConfig]:
    """Return provider choices, with JSON config overriding built-in templates."""
    providers = [_legacy_provider(settings_obj)]
    seen = {providers[0].id}

    for item in _builtin_provider_items(settings_obj):
        provider = _provider_from_dict(item, settings_obj=settings_obj, source="builtin")
        if provider is None or provider.id in seen:
            continue
        providers.append(provider)
        seen.add(provider.id)

    for item in _raw_provider_items(settings_obj):
        provider = _provider_from_dict(item, settings_obj=settings_obj, source="json")
        if provider is None:
            continue
        if provider.id in seen:
            providers = [provider if existing.id == provider.id else existing for existing in providers]
        else:
            providers.append(provider)
            seen.add(provider.id)
    return providers


def get_llm_provider(provider_id: Optional[str] = None, settings_obj: Any = settings) -> LLMProviderConfig:
    """Resolve one provider by id; when omitted, use settings' default provider id."""
    providers = list_llm_providers(settings_obj)
    fallback_id = _clean_str(_safe_get(settings_obj, "llm_default_provider_id", ""), providers[0].id)
    target_id = _clean_str(provider_id, fallback_id)
    for provider in providers:
        if provider.id == target_id:
            return provider
    raise ValueError(f"unknown LLM provider: {target_id}")
