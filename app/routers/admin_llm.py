"""Admin LLM provider management routes."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.db import (
    clear_llm_provider_override_id,
    get_llm_provider_override_id,
    set_llm_provider_override_id,
)
from app.llm_adapters import chat_completion
from app.llm_providers import LLMProviderConfig, list_llm_providers
from app.routers.deps import get_admin_user, verify_admin_auth


logger = logging.getLogger("ai4all")
router = APIRouter()


class ActiveLLMProviderRequest(BaseModel):
    provider_id: str


class LLMProviderProbeRequest(BaseModel):
    message: Optional[str] = Field(default=None, max_length=2000)


def _settings_default_provider_id() -> str:
    value = getattr(settings, "llm_default_provider_id", "") or ""
    return str(value).strip() or "deepseek-v4-pro"


def _load_provider_state() -> tuple[list[LLMProviderConfig], Optional[str], str, LLMProviderConfig]:
    try:
        providers = list_llm_providers(settings)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    runtime_override_id = get_llm_provider_override_id()
    settings_default_id = _settings_default_provider_id()
    active_id = runtime_override_id or settings_default_id
    by_id = {provider.id: provider for provider in providers}
    active_provider = by_id.get(active_id) or by_id.get(settings_default_id) or providers[0]
    return providers, runtime_override_id, settings_default_id, active_provider


def _provider_for_admin(
    provider: LLMProviderConfig,
    *,
    active_id: str,
    settings_default_id: str,
    runtime_override_id: Optional[str],
) -> dict:
    item = provider.redacted()
    item["active"] = provider.id == active_id
    item["settings_default"] = provider.id == settings_default_id
    item["runtime_override"] = provider.id == runtime_override_id
    return item


def _extract_reply_text(payload: dict) -> str:
    message = (payload.get("choices") or [{}])[0].get("message") or {}
    return str(message.get("content") or "").strip()


@router.get("/admin/llm/providers")
def admin_list_llm_providers(_: None = Depends(verify_admin_auth)) -> dict:
    providers, runtime_override_id, settings_default_id, active_provider = _load_provider_state()
    return {
        "active_provider_id": active_provider.id,
        "effective_provider_id": active_provider.id,
        "stored_active_provider_id": runtime_override_id,
        "runtime_override_provider_id": runtime_override_id,
        "default_provider_id": settings_default_id,
        "settings_default_provider_id": settings_default_id,
        "active_provider": active_provider.redacted(),
        "effective_provider": active_provider.redacted(),
        "providers": [
            _provider_for_admin(
                provider,
                active_id=active_provider.id,
                settings_default_id=settings_default_id,
                runtime_override_id=runtime_override_id,
            )
            for provider in providers
        ],
    }


@router.patch("/admin/llm/active-provider")
def admin_set_active_llm_provider(
    payload: ActiveLLMProviderRequest,
    admin_user: dict = Depends(get_admin_user),
) -> dict:
    if admin_user.get("role") not in {"admin", "staff"}:
        raise HTTPException(status_code=403, detail="admin or staff role required")
    providers, _, settings_default_id, _ = _load_provider_state()
    requested_provider_id = str(payload.provider_id or "").strip()
    target = next((provider for provider in providers if provider.id == requested_provider_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="provider not found")
    if not target.enabled:
        raise HTTPException(status_code=400, detail="provider is disabled")
    if not target.api_key:
        raise HTTPException(status_code=400, detail="provider API key is not configured")
    if target.id == settings_default_id:
        clear_llm_provider_override_id()
        runtime_override_id = None
    else:
        runtime_override_id = set_llm_provider_override_id(
            target.id,
            updated_by=str(admin_user.get("id") or admin_user.get("role") or "admin"),
        )
    return {
        "status": "ok",
        "active_provider_id": target.id,
        "effective_provider_id": target.id,
        "runtime_override_provider_id": runtime_override_id,
        "default_provider_id": settings_default_id,
        "settings_default_provider_id": settings_default_id,
        "active_provider": target.redacted(),
        "effective_provider": target.redacted(),
    }


@router.delete("/admin/llm/active-provider")
def admin_clear_active_llm_provider(
    admin_user: dict = Depends(get_admin_user),
) -> dict:
    if admin_user.get("role") not in {"admin", "staff"}:
        raise HTTPException(status_code=403, detail="admin or staff role required")
    clear_llm_provider_override_id()
    providers, runtime_override_id, settings_default_id, active_provider = _load_provider_state()
    return {
        "status": "ok",
        "active_provider_id": active_provider.id,
        "effective_provider_id": active_provider.id,
        "runtime_override_provider_id": runtime_override_id,
        "default_provider_id": settings_default_id,
        "settings_default_provider_id": settings_default_id,
        "active_provider": active_provider.redacted(),
        "effective_provider": active_provider.redacted(),
    }


@router.post("/admin/llm/providers/{provider_id}/probe")
def admin_probe_llm_provider(
    provider_id: str,
    payload: LLMProviderProbeRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    providers, _, _, _ = _load_provider_state()
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
