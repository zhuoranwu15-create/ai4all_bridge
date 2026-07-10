"""Admin LLM provider management routes (family × tier model).

运营可切换 active family（一键把两档设为该家族默认），或对单个 tier 覆盖到任意 provider（含跨家族）。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.db import (
    clear_tier_provider_override,
    get_llm_runtime_bindings,
    set_active_family,
    set_tier_provider_override,
)
from app.llm_adapters import chat_completion
from app.llm_providers import (
    SUPPORTED_TIERS,
    TIER_FLASH,
    TIER_PRO,
    LLMProviderConfig,
    list_llm_providers,
    resolve_provider_for_tier,
)
from app.routers.deps import get_admin_user, verify_admin_auth


logger = logging.getLogger("ai4all")
router = APIRouter()


class ActiveFamilyRequest(BaseModel):
    family: str


class TierOverrideRequest(BaseModel):
    provider_id: str


class LLMProviderProbeRequest(BaseModel):
    message: Optional[str] = Field(default=None, max_length=2000)


def _settings_default_family() -> str:
    value = getattr(settings, "llm_active_family", "") or ""
    return str(value).strip() or "deepseek"


def _load_state() -> tuple[list[LLMProviderConfig], dict, str]:
    try:
        providers = list_llm_providers(settings)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    bindings = get_llm_runtime_bindings()
    active_family = bindings.get("active_family") or _settings_default_family()
    return providers, bindings, active_family


def _resolved_for_tier(tier: str, bindings: dict, active_family: str) -> LLMProviderConfig:
    override_key = "pro_provider_id" if tier == TIER_PRO else "flash_provider_id"
    return resolve_provider_for_tier(
        tier,
        family=active_family,
        override_provider_id=bindings.get(override_key),
        settings_obj=settings,
    )


def _provider_for_admin(
    provider: LLMProviderConfig,
    *,
    active_family: str,
    pro_id: str,
    flash_id: str,
    bindings: dict,
) -> dict:
    item = provider.redacted()
    item["active_family"] = provider.family == active_family
    item["serves_pro"] = provider.id == pro_id
    item["serves_flash"] = provider.id == flash_id
    item["pro_override"] = provider.id == bindings.get("pro_provider_id")
    item["flash_override"] = provider.id == bindings.get("flash_provider_id")
    return item


def _state_response(status: Optional[str] = None) -> dict:
    providers, bindings, active_family = _load_state()
    pro_provider = _resolved_for_tier(TIER_PRO, bindings, active_family)
    flash_provider = _resolved_for_tier(TIER_FLASH, bindings, active_family)
    body = {
        "active_family": active_family,
        "settings_default_family": _settings_default_family(),
        "pro_provider": pro_provider.redacted(),
        "flash_provider": flash_provider.redacted(),
        "pro_override_provider_id": bindings.get("pro_provider_id"),
        "flash_override_provider_id": bindings.get("flash_provider_id"),
        "providers": [
            _provider_for_admin(
                provider,
                active_family=active_family,
                pro_id=pro_provider.id,
                flash_id=flash_provider.id,
                bindings=bindings,
            )
            for provider in providers
        ],
    }
    if status is not None:
        body["status"] = status
    return body


def _extract_reply_text(payload: dict) -> str:
    message = (payload.get("choices") or [{}])[0].get("message") or {}
    return str(message.get("content") or "").strip()


def _require_admin_or_staff(admin_user: dict) -> None:
    if admin_user.get("role") not in {"admin", "staff"}:
        raise HTTPException(status_code=403, detail="admin or staff role required")


@router.get("/admin/llm/providers")
def admin_list_llm_providers(_: None = Depends(verify_admin_auth)) -> dict:
    return _state_response()


@router.patch("/admin/llm/active-family")
def admin_set_active_family(
    payload: ActiveFamilyRequest,
    admin_user: dict = Depends(get_admin_user),
) -> dict:
    _require_admin_or_staff(admin_user)
    providers, _, _ = _load_state()
    requested = str(payload.family or "").strip()
    families = {provider.family for provider in providers if provider.family}
    if requested not in families:
        raise HTTPException(status_code=404, detail="family not found")
    # 切家族会清掉两档 override，落到该家族默认 → 必须确保解析出的 pro/flash provider 都已配 key，
    # 否则会把主对话/后台任务切到一个没 key 的 provider 而静默失败。
    for tier in (TIER_PRO, TIER_FLASH):
        resolved = resolve_provider_for_tier(tier, family=requested, settings_obj=settings)
        if not resolved.enabled:
            raise HTTPException(status_code=400, detail=f"{tier} provider is disabled for family {requested}")
        if not resolved.api_key:
            raise HTTPException(
                status_code=400,
                detail=f"{tier} provider API key is not configured for family {requested}",
            )
    set_active_family(
        requested,
        updated_by=str(admin_user.get("id") or admin_user.get("role") or "admin"),
    )
    return _state_response(status="ok")


@router.patch("/admin/llm/tier-override/{tier}")
def admin_set_tier_override(
    tier: str,
    payload: TierOverrideRequest,
    admin_user: dict = Depends(get_admin_user),
) -> dict:
    _require_admin_or_staff(admin_user)
    clean_tier = str(tier or "").strip().lower()
    if clean_tier not in SUPPORTED_TIERS:
        raise HTTPException(status_code=404, detail="unknown tier")
    providers, _, _ = _load_state()
    requested_provider_id = str(payload.provider_id or "").strip()
    target = next((p for p in providers if p.id == requested_provider_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="provider not found")
    if not target.enabled:
        raise HTTPException(status_code=400, detail="provider is disabled")
    if not target.api_key:
        raise HTTPException(status_code=400, detail="provider API key is not configured")
    set_tier_provider_override(
        clean_tier,
        target.id,
        updated_by=str(admin_user.get("id") or admin_user.get("role") or "admin"),
    )
    return _state_response(status="ok")


@router.delete("/admin/llm/tier-override/{tier}")
def admin_clear_tier_override(
    tier: str,
    admin_user: dict = Depends(get_admin_user),
) -> dict:
    _require_admin_or_staff(admin_user)
    clean_tier = str(tier or "").strip().lower()
    if clean_tier not in SUPPORTED_TIERS:
        raise HTTPException(status_code=404, detail="unknown tier")
    clear_tier_provider_override(clean_tier)
    return _state_response(status="ok")


@router.post("/admin/llm/providers/{provider_id}/probe")
def admin_probe_llm_provider(
    provider_id: str,
    payload: LLMProviderProbeRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    providers, _, _ = _load_state()
    target = next((provider for provider in providers if provider.id == provider_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="provider not found")
    if not target.enabled:
        raise HTTPException(status_code=400, detail="provider is disabled")
    if not target.api_key:
        raise HTTPException(status_code=400, detail="provider API key is not configured")
    started_message = (payload.message or "").strip() or "你好，测试一下连通性。"
    try:
        response = chat_completion(
            target,
            [{"role": "user", "content": started_message}],
        )
        reply = _extract_reply_text(response)
    except Exception as err:
        logger.exception("llm provider probe failed provider=%s error=%s", provider_id, err)
        raise HTTPException(status_code=502, detail=str(err)) from err
    return {
        "status": "ok",
        "provider_id": target.id,
        "model": target.model,
        "reply": reply,
    }
