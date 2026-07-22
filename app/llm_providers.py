"""LLM provider configuration resolution.

The rest of the application talks to app.llm only.  This module keeps provider
selection and protocol/model details out of turn/proactive/dreaming code.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from app.config import settings


logger = logging.getLogger("ai4all.llm")

SUPPORTED_LLM_PROTOCOLS = {"openai_chat", "openai_responses", "anthropic_messages"}
_DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
_DEFAULT_FAMILY = "deepseek"

# ---- 两层模型：档位（tier）与任务→档位路由（task→tier） --------------------
TIER_PRO = "pro"
TIER_FLASH = "flash"
SUPPORTED_TIERS = {TIER_PRO, TIER_FLASH}

# 调用点声明的 task kind 常量（避免各处拼裸字符串）。
TASK_MAIN_REPLY = "main_reply"
TASK_ONBOARDING_EXTRACTION = "onboarding_extraction"
TASK_MODERATION = "moderation"
TASK_ROLLING_SUMMARY = "rolling_summary"
TASK_DREAMING = "dreaming"
TASK_USER_META = "user_meta"
TASK_RELATIONSHIP_STATE = "relationship_state"
TASK_PROACTIVE_RECALL = "proactive_recall"
TASK_WEB_COMPLETION = "web_completion"
TASK_WORLD_CONTENT = "world_content"

# 默认路由：主对话用 pro，后台任务用 flash。可被 settings.llm_task_tiers(JSON) 逐项覆盖。
_TASK_TIER_DEFAULTS: Dict[str, str] = {
    TASK_MAIN_REPLY: TIER_PRO,
    TASK_ONBOARDING_EXTRACTION: TIER_FLASH,
    TASK_MODERATION: TIER_FLASH,
    TASK_ROLLING_SUMMARY: TIER_FLASH,
    TASK_DREAMING: TIER_FLASH,
    TASK_USER_META: TIER_FLASH,
    TASK_RELATIONSHIP_STATE: TIER_FLASH,
    TASK_PROACTIVE_RECALL: TIER_FLASH,
    TASK_WEB_COMPLETION: TIER_FLASH,
    TASK_WORLD_CONTENT: TIER_FLASH,
}
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
    family: str = ""
    tier: str = TIER_PRO
    enabled: bool = True
    supports_tools: bool = True
    timeout_seconds: float = 50.0
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
            "family": self.family,
            "tier": self.tier,
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
    family = _clean_str(raw.get("family"))
    tier = _clean_str(raw.get("tier"), TIER_PRO).lower()
    if tier not in SUPPORTED_TIERS:
        raise ValueError(f"unsupported LLM tier for provider {provider_id!r}: {tier}")
    return LLMProviderConfig(
        id=provider_id,
        label=label,
        protocol=protocol,
        base_url=base_url,
        model=model,
        api_key=api_key,
        api_key_env=api_key_env,
        family=family,
        tier=tier,
        enabled=_clean_bool(raw.get("enabled"), True),
        supports_tools=_clean_bool(raw.get("supports_tools"), True),
        timeout_seconds=max(1.0, _clean_float(raw.get("timeout_seconds"), _clean_float(_safe_get(settings_obj, "llm_timeout_seconds", 50.0), 50.0))),
        connect_timeout_seconds=max(0.1, _clean_float(raw.get("connect_timeout_seconds"), _clean_float(_safe_get(settings_obj, "llm_connect_timeout_seconds", 5.0), 5.0))),
        max_retries=max(0, _clean_int(raw.get("max_retries"), _clean_int(_safe_get(settings_obj, "llm_max_retries", 1), 1))),
        force_ipv4=_clean_bool(raw.get("force_ipv4"), _clean_bool(_safe_get(settings_obj, "llm_force_ipv4", True), True)),
        temperature=_clean_float(raw.get("temperature"), 0.7),
        max_output_tokens=max(1, _clean_int(raw.get("max_output_tokens"), 1024)),
        source=source,
    )


def _model_provider_item(
    *,
    provider_id: str,
    label: str,
    protocol: str,
    base_url: str,
    api_key_env: str,
    model: str,
    family: str,
    tier: str,
    supports_tools: bool = True,
) -> Dict[str, Any]:
    """Create a provider entry for one selectable family×tier cell."""
    return {
        "id": provider_id,
        "label": label,
        "protocol": protocol,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "model": model,
        "family": family,
        "tier": tier,
        "supports_tools": supports_tools,
    }


def _builtin_provider_items(settings_obj: Any) -> List[Dict[str, Any]]:
    """Built-in family×tier matrix, exposed in admin even before JSON overrides.

    deepseek 内置 pro+flash 两档（已知可用对）；openai/anthropic 各给 pro 一档，
    flash 由 LLM_PROVIDERS_JSON 补（每条带 family/tier）。
    """
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
            provider_id="deepseek",
            label="DeepSeek V4 Flash",
            protocol="openai_chat",
            base_url=deepseek_base_url,
            api_key_env="LLM_API_KEY",
            model=_DEFAULT_DEEPSEEK_MODEL,
            family="deepseek",
            tier=TIER_FLASH,
        ),
        _model_provider_item(
            provider_id="deepseek-v4-pro",
            label="DeepSeek V4 Pro",
            protocol="openai_chat",
            base_url=deepseek_base_url,
            api_key_env="LLM_API_KEY",
            model="deepseek-v4-pro",
            family="deepseek",
            tier=TIER_PRO,
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
            family="openai",
            tier=TIER_PRO,
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
            family="anthropic",
            tier=TIER_PRO,
        ),
    ]


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
    """Return the family×tier provider matrix, with JSON entries overriding built-ins by id.

    JSON 条目必须自带 family/tier（省略则默认 family=""、tier="pro"）——覆盖内置 id 时也要显式声明，
    不做隐式继承，避免"改了 model 却忘了 tier"这类隐蔽错配。
    """
    providers: List[LLMProviderConfig] = []
    seen: Dict[str, int] = {}
    for item in _builtin_provider_items(settings_obj):
        provider = _provider_from_dict(item, settings_obj=settings_obj, source="builtin")
        if provider is None or provider.id in seen:
            continue
        seen[provider.id] = len(providers)
        providers.append(provider)

    for item in _raw_provider_items(settings_obj):
        provider = _provider_from_dict(item, settings_obj=settings_obj, source="json")
        if provider is None:
            continue
        if provider.id in seen:
            providers[seen[provider.id]] = provider
        else:
            seen[provider.id] = len(providers)
            providers.append(provider)
    return providers


def get_llm_provider(provider_id: Optional[str] = None, settings_obj: Any = settings) -> LLMProviderConfig:
    """Resolve one provider by explicit id; when omitted, use the active family's pro tier."""
    providers = list_llm_providers(settings_obj)
    target_id = _clean_str(provider_id)
    if not target_id:
        return resolve_provider_for_tier(TIER_PRO, settings_obj=settings_obj)
    for provider in providers:
        if provider.id == target_id:
            return provider
    raise ValueError(f"unknown LLM provider: {target_id}")


def _safe_list_providers(settings_obj: Any) -> List[LLMProviderConfig]:
    """list_llm_providers, but on invalid JSON fall back to the built-in matrix (runtime resilience)."""
    try:
        return list_llm_providers(settings_obj)
    except ValueError as err:
        logger.error("LLM_PROVIDERS_JSON invalid (%s); falling back to built-in matrix only", err)
        out: List[LLMProviderConfig] = []
        seen = set()
        for item in _builtin_provider_items(settings_obj):
            provider = _provider_from_dict(item, settings_obj=settings_obj, source="builtin")
            if provider is not None and provider.id not in seen:
                seen.add(provider.id)
                out.append(provider)
        return out


def resolve_provider_for_tier(
    tier: str,
    *,
    family: Optional[str] = None,
    override_provider_id: Optional[str] = None,
    settings_obj: Any = settings,
) -> LLMProviderConfig:
    """Resolve the provider serving a tier: per-tier override → active family × tier → fallbacks.

    override_provider_id / family 由调用方（app.llm）从运行时绑定读出后传入，本模块保持无 DB 依赖。
    """
    tier = tier if tier in SUPPORTED_TIERS else TIER_PRO
    providers = _safe_list_providers(settings_obj)
    if not providers:
        raise ValueError("no LLM providers configured")

    # 1. 该 tier 若有运行时 override provider id，直接返回该 provider。
    override_id = _clean_str(override_provider_id)
    if override_id:
        for provider in providers:
            if provider.id == override_id:
                return provider
        logger.warning("llm tier override provider not found id=%s tier=%s", override_id, tier)

    # 2. 确定 active family。
    fam = _clean_str(family) or _clean_str(_safe_get(settings_obj, "llm_active_family", "")) or _DEFAULT_FAMILY

    # 3. 命中 (family, tier)；否则依次兜底：该 family 的 pro → 任意同 tier → 列表首个。
    for provider in providers:
        if provider.family == fam and provider.tier == tier:
            return provider
    for provider in providers:
        if provider.family == fam and provider.tier == TIER_PRO:
            return provider
    for provider in providers:
        if provider.tier == tier:
            return provider
    return providers[0]


def _parse_task_tiers(settings_obj: Any) -> Dict[str, str]:
    raw = _clean_str(_safe_get(settings_obj, "llm_task_tiers", ""))
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("LLM_TASK_TIERS is not valid JSON; ignoring")
        return {}
    if not isinstance(data, dict):
        logger.warning("LLM_TASK_TIERS must be a JSON object; ignoring")
        return {}
    out: Dict[str, str] = {}
    for key, value in data.items():
        tier = str(value).strip().lower()
        if tier in SUPPORTED_TIERS:
            out[str(key)] = tier
        else:
            logger.warning("LLM_TASK_TIERS ignoring unknown tier %r for task %r", value, key)
    return out


def tier_for_task(task_kind: str, *, settings_obj: Any = settings) -> str:
    """Map a task kind to a tier: default table (main_reply=pro, 其余=flash) overridden by LLM_TASK_TIERS."""
    default = _TASK_TIER_DEFAULTS.get(task_kind)
    if default is None:
        logger.warning("tier_for_task unknown task kind=%s; defaulting to flash", task_kind)
        default = TIER_FLASH
    return _parse_task_tiers(settings_obj).get(task_kind, default)
