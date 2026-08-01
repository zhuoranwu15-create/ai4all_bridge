"""朝夕创建者角色模板管理、公共链接预览与 owner-scoped 统计 API。"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from app.config import settings
from app.db import SessionPrincipal, get_campaign_funnel, validate_referral_code
from app.platform.quota.rate_limiter import RateLimiter
from app.products.zhaoxi.application.campaign_stats import (
    resolve_campaign_stats_range,
)
from app.products.zhaoxi.application.creator_role_template_links import (
    create_creator_role_template_version,
    disable_creator_role_template,
    effective_creator_role_template_status,
    enable_creator_role_template,
    get_creator_personal_invite_code,
    publish_creator_role_template,
    require_creator_role_template_eligibility,
)
from app.products.zhaoxi.application.creator_role_template_review import (
    review_creator_role_template_version,
)
from app.products.zhaoxi.domain.creator_role_templates import (
    ZHAOXI_APP_ID,
    CreatorRoleTemplate,
    CreatorRoleTemplateError,
    is_creator_role_template_campaign_code,
)
from app.products.zhaoxi.infrastructure.persistence.creator_role_templates import (
    create_creator_role_template,
    get_creator_role_template,
    get_creator_role_template_by_campaign_code,
    list_creator_role_template_versions,
    list_creator_role_templates,
    soft_delete_creator_role_template,
)
from app.routers.deps import _require_session

router = APIRouter()
_preview_limiter = RateLimiter()
_PREVIEW_RPM = 60
_PREVIEW_CHARS = 160


class CreatorRoleTemplateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai_name: str
    personality_text: str
    mission_text: str


class CreatorRoleTemplateUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai_name: Optional[str] = None
    personality_text: Optional[str] = None
    mission_text: Optional[str] = None


class CreatorRoleTemplatePublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: str


def _require_zhaoxi_principal(principal: SessionPrincipal) -> SessionPrincipal:
    if principal.app_id != ZHAOXI_APP_ID:
        raise HTTPException(status_code=403, detail="creator_role_template_wrong_app")
    return principal


def _require_mutations_enabled() -> None:
    if not bool(getattr(settings, "creator_role_templates_enabled", False)):
        raise HTTPException(
            status_code=503,
            detail="creator_role_templates_disabled",
        )


def _raise_domain_error(exc: CreatorRoleTemplateError) -> None:
    code = exc.code
    if code == "creator_role_template_not_found":
        status_code = 404
    elif code == "creator_role_template_not_eligible":
        status_code = 403
    elif code in {
        "creator_role_template_limit_reached",
        "creator_role_template_state_conflict",
        "creator_role_template_disabled_by_admin",
        "creator_role_template_expired",
        "role_review_in_progress",
        "role_review_pending",
    }:
        status_code = 409
    else:
        status_code = 400
    raise HTTPException(status_code=status_code, detail=code) from exc


def _owned_template(principal: SessionPrincipal, template_id: str) -> CreatorRoleTemplate:
    _require_zhaoxi_principal(principal)
    template = get_creator_role_template(
        creator_platform_user_id=principal.platform_user_id,
        app_id=principal.app_id,
        template_id=template_id,
    )
    if template is None:
        raise HTTPException(status_code=404, detail="creator_role_template_not_found")
    return template


def _require_eligible(principal: SessionPrincipal) -> None:
    """把 application 资格错误稳定映射为 Web HTTP 错误。"""
    try:
        require_creator_role_template_eligibility(
            creator_platform_user_id=principal.platform_user_id,
            app_id=principal.app_id,
        )
    except CreatorRoleTemplateError as exc:
        _raise_domain_error(exc)


def _version_dict(version) -> Dict[str, Any]:
    value = asdict(version)
    value["is_published"] = bool(value["is_published"])
    return value


def _template_response(
    template: CreatorRoleTemplate,
    *,
    include_versions: bool,
) -> Dict[str, Any]:
    versions = list_creator_role_template_versions(
        creator_platform_user_id=template.creator_platform_user_id,
        app_id=template.app_id,
        template_id=template.id,
    )
    published = next((version for version in versions if version.is_published), None)
    latest = versions[0] if versions else None
    effective_status = effective_creator_role_template_status(template)
    invite_code = get_creator_personal_invite_code(
        creator_platform_user_id=template.creator_platform_user_id,
        app_id=template.app_id,
    )
    registration_url = None
    if effective_status == "active" and published is not None and invite_code:
        registration_url = (
            "/?invite_code="
            + quote(invite_code, safe="")
            + "&campaign_code="
            + quote(template.campaign_code, safe="")
        )
    result: Dict[str, Any] = {
        "id": template.id,
        "slot_no": template.slot_no,
        "campaign_code": template.campaign_code,
        "status": template.status,
        "effective_status": effective_status,
        "used_count": template.used_count,
        "activated_at": template.activated_at,
        "expires_at": template.expires_at,
        "created_at": template.created_at,
        "updated_at": template.updated_at,
        "registration_url": registration_url,
        "published_version": _version_dict(published) if published else None,
        "latest_version": _version_dict(latest) if latest else None,
    }
    if include_versions:
        result["versions"] = [_version_dict(version) for version in versions]
    return result


@router.get("/web/me/creator-role-templates")
def creator_role_template_list(
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    _require_zhaoxi_principal(principal)
    templates = list_creator_role_templates(
        creator_platform_user_id=principal.platform_user_id,
        app_id=principal.app_id,
        limit=3,
    )
    return {
        "creator_role_templates": [
            _template_response(template, include_versions=False)
            for template in templates
        ]
    }


@router.post("/web/me/creator-role-templates")
def creator_role_template_create(
    payload: CreatorRoleTemplateCreateRequest,
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    _require_zhaoxi_principal(principal)
    _require_mutations_enabled()
    _require_eligible(principal)
    try:
        created = create_creator_role_template(
            creator_platform_user_id=principal.platform_user_id,
            app_id=principal.app_id,
            ai_name=payload.ai_name,
            personality_text=payload.personality_text,
            mission_text=payload.mission_text,
        )
        review_creator_role_template_version(
            creator_platform_user_id=principal.platform_user_id,
            app_id=principal.app_id,
            template_id=created.template.id,
            version_id=created.version.id,
        )
        template = _owned_template(principal, created.template.id)
        return {"creator_role_template": _template_response(template, include_versions=True)}
    except CreatorRoleTemplateError as exc:
        _raise_domain_error(exc)


@router.get("/web/me/creator-role-templates/{template_id}")
def creator_role_template_detail(
    template_id: str,
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    template = _owned_template(principal, template_id)
    return {"creator_role_template": _template_response(template, include_versions=True)}


@router.patch("/web/me/creator-role-templates/{template_id}")
def creator_role_template_update(
    template_id: str,
    payload: CreatorRoleTemplateUpdateRequest,
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    _owned_template(principal, template_id)
    _require_mutations_enabled()
    _require_eligible(principal)
    try:
        version = create_creator_role_template_version(
            creator_platform_user_id=principal.platform_user_id,
            app_id=principal.app_id,
            template_id=template_id,
            **payload.model_dump(exclude_unset=True),
        )
        review_creator_role_template_version(
            creator_platform_user_id=principal.platform_user_id,
            app_id=principal.app_id,
            template_id=template_id,
            version_id=version.id,
        )
        template = _owned_template(principal, template_id)
        return {"creator_role_template": _template_response(template, include_versions=True)}
    except CreatorRoleTemplateError as exc:
        _raise_domain_error(exc)


@router.post("/web/me/creator-role-templates/{template_id}/review")
def creator_role_template_review(
    template_id: str,
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    template = _owned_template(principal, template_id)
    _require_mutations_enabled()
    _require_eligible(principal)
    versions = list_creator_role_template_versions(
        creator_platform_user_id=principal.platform_user_id,
        app_id=principal.app_id,
        template_id=template.id,
    )
    pending = next((v for v in versions if v.review_status == "pending"), None)
    if pending is None:
        raise HTTPException(status_code=409, detail="role_review_not_pending")
    try:
        review_creator_role_template_version(
            creator_platform_user_id=principal.platform_user_id,
            app_id=principal.app_id,
            template_id=template.id,
            version_id=pending.id,
        )
        current = _owned_template(principal, template.id)
        return {"creator_role_template": _template_response(current, include_versions=True)}
    except CreatorRoleTemplateError as exc:
        _raise_domain_error(exc)


@router.post("/web/me/creator-role-templates/{template_id}/publish")
def creator_role_template_publish(
    template_id: str,
    payload: CreatorRoleTemplatePublishRequest,
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    _owned_template(principal, template_id)
    _require_mutations_enabled()
    _require_eligible(principal)
    try:
        mutation = publish_creator_role_template(
            creator_platform_user_id=principal.platform_user_id,
            app_id=principal.app_id,
            template_id=template_id,
            version_id=payload.version_id,
        )
        return {
            "creator_role_template": _template_response(
                mutation.template,
                include_versions=True,
            )
        }
    except CreatorRoleTemplateError as exc:
        _raise_domain_error(exc)


@router.post("/web/me/creator-role-templates/{template_id}/disable")
def creator_role_template_disable(
    template_id: str,
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    _owned_template(principal, template_id)
    try:
        template = disable_creator_role_template(
            creator_platform_user_id=principal.platform_user_id,
            app_id=principal.app_id,
            template_id=template_id,
        )
        return {"creator_role_template": _template_response(template, include_versions=True)}
    except CreatorRoleTemplateError as exc:
        _raise_domain_error(exc)


@router.post("/web/me/creator-role-templates/{template_id}/enable")
def creator_role_template_enable(
    template_id: str,
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    _owned_template(principal, template_id)
    _require_mutations_enabled()
    _require_eligible(principal)
    try:
        template = enable_creator_role_template(
            creator_platform_user_id=principal.platform_user_id,
            app_id=principal.app_id,
            template_id=template_id,
        )
        return {"creator_role_template": _template_response(template, include_versions=True)}
    except CreatorRoleTemplateError as exc:
        _raise_domain_error(exc)


@router.delete("/web/me/creator-role-templates/{template_id}")
def creator_role_template_delete(
    template_id: str,
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    _owned_template(principal, template_id)
    try:
        template = soft_delete_creator_role_template(
            creator_platform_user_id=principal.platform_user_id,
            app_id=principal.app_id,
            template_id=template_id,
        )
        return {"creator_role_template": _template_response(template, include_versions=True)}
    except CreatorRoleTemplateError as exc:
        _raise_domain_error(exc)


@router.get("/web/me/creator-role-templates/{template_id}/stats")
def creator_role_template_stats(
    template_id: str,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    template = _owned_template(principal, template_id)
    try:
        resolved_from, resolved_to = resolve_campaign_stats_range(date_from, date_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "ok",
        **get_campaign_funnel(
            campaign_code=template.campaign_code,
            date_from=resolved_from,
            date_to=resolved_to,
        ),
    }


def _preview_text(value: str) -> str:
    text = str(value or "").strip()
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "…"


@router.get("/web/creator-role-template-links/{campaign_code}/preview")
def creator_role_template_public_preview(
    campaign_code: str,
    request: Request,
    invite_code: Optional[str] = None,
) -> dict:
    client_host = request.client.host if request.client else "unknown"
    if not _preview_limiter.check_rpm(
        f"creator-role-preview:{client_host}",
        _PREVIEW_RPM,
        window_seconds=60.0,
    ):
        return {"valid": False, "reason": "rate_limited"}
    if not bool(getattr(settings, "creator_role_templates_enabled", False)):
        return {"valid": False, "reason": "capability_disabled"}
    if not is_creator_role_template_campaign_code(campaign_code):
        return {"valid": False, "reason": "invalid_code"}
    source = get_creator_role_template_by_campaign_code(campaign_code=campaign_code)
    if source is None:
        return {"valid": False, "reason": "not_found"}
    template = CreatorRoleTemplate.from_row(source)
    effective_status = effective_creator_role_template_status(template)
    if effective_status != "active":
        return {"valid": False, "reason": effective_status}
    if source.get("published_version_id") is None:
        return {"valid": False, "reason": "no_published_version"}
    invitation = validate_referral_code(
        code=invite_code,
        expected_app_id=ZHAOXI_APP_ID,
    )
    if not invitation.get("valid"):
        return {"valid": False, "reason": "invalid_invite"}
    referral = invitation.get("referral_code") or {}
    if referral.get("platform_user_id") != template.creator_platform_user_id:
        return {"valid": False, "reason": "creator_mismatch"}
    return {
        "valid": True,
        "role": {
            "name": source["published_ai_name"],
            "personality_preview": _preview_text(
                source["published_personality_text"]
            ),
            "mission_preview": _preview_text(source["published_mission_text"]),
            "expires_at": template.expires_at,
        },
    }


__all__ = ["router"]
