"""朝夕创建者角色模板管理、公共链接预览与 owner-scoped 统计 API。"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.bootstrap.product_registry import PRODUCTION_PRODUCT_REGISTRY
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
from app.products.zhaoxi.application.creator_role_template_localization import (
    creator_role_template_review_reason_display,
    creator_role_template_review_status_display,
    creator_role_template_summary_edit_success_display,
    creator_role_template_summary_rejection_display,
    creator_role_template_summary_review_unavailable_display,
    creator_role_template_status_display,
)
from app.products.zhaoxi.application.creator_role_template_review import (
    review_creator_role_template_summary_version,
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
    list_creator_role_template_summary_review_runs,
    list_creator_role_template_versions,
    list_creator_role_templates,
    soft_delete_creator_role_template,
)
from app.routers.deps import _require_session

router = APIRouter()
_preview_limiter = RateLimiter()
_PREVIEW_RPM = 60
class CreatorRoleTemplateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai_name: str
    personality_text: str
    mission_text: str
    opening_line: str


class CreatorRoleTemplateUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai_name: Optional[str] = None
    personality_text: Optional[str] = None
    mission_text: Optional[str] = None
    opening_line: Optional[str] = None


class CreatorRoleTemplateSummaryEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: str
    summary: str


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
        "creator_role_template_summary_edit_unavailable",
        "creator_role_template_summary_review_in_progress",
        "creator_role_template_summary_result_stale",
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


def _version_dict(
    version,
    *,
    template: CreatorRoleTemplate,
    language: str,
) -> Dict[str, Any]:
    value = asdict(version)
    value["is_published"] = bool(value["is_published"])
    # 原始 LLM 理由只用于数据库与管理后台审计；用户接口仅返回确定性本地化理由。
    value.pop("review_reason", None)
    value["review_status_display"] = creator_role_template_review_status_display(
        value["review_status"], language
    )
    value["review_reason_display"] = creator_role_template_review_reason_display(
        value["review_categories_json"],
        review_status=value["review_status"],
        language=language,
    )
    summary_runs = list_creator_role_template_summary_review_runs(
        creator_platform_user_id=template.creator_platform_user_id,
        app_id=template.app_id,
        template_id=template.id,
        version_id=version.id,
    )
    latest_summary_run = summary_runs[0] if summary_runs else None
    value["summary_review_reason_display"] = (
        creator_role_template_summary_rejection_display(
            latest_summary_run.get("categories_json"),
            language=language,
        )
        if latest_summary_run and latest_summary_run.get("status") == "rejected"
        else ""
    )
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
    language = PRODUCTION_PRODUCT_REGISTRY.require_enabled(
        template.app_id
    ).default_language
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
        "status_display": creator_role_template_status_display(
            template.status, language
        ),
        "effective_status": effective_status,
        "effective_status_display": creator_role_template_status_display(
            effective_status, language
        ),
        "used_count": template.used_count,
        "activated_at": template.activated_at,
        "expires_at": template.expires_at,
        "created_at": template.created_at,
        "updated_at": template.updated_at,
        "registration_url": registration_url,
        "published_version": (
            _version_dict(published, template=template, language=language)
            if published
            else None
        ),
        "latest_version": (
            _version_dict(latest, template=template, language=language)
            if latest
            else None
        ),
    }
    if include_versions:
        result["versions"] = [
            _version_dict(version, template=template, language=language)
            for version in versions
        ]
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
            opening_line=payload.opening_line,
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


@router.post("/web/me/creator-role-templates/{template_id}/summary-edit")
def creator_role_template_summary_edit(
    template_id: str,
    payload: CreatorRoleTemplateSummaryEditRequest,
    principal: SessionPrincipal = Depends(_require_session),
):
    """审核并提交未发布版本唯一一次正式简介修改。"""

    template = _owned_template(principal, template_id)
    _require_mutations_enabled()
    _require_eligible(principal)
    language = PRODUCTION_PRODUCT_REGISTRY.require_enabled(
        template.app_id
    ).default_language
    try:
        outcome = review_creator_role_template_summary_version(
            creator_platform_user_id=principal.platform_user_id,
            app_id=principal.app_id,
            template_id=template_id,
            version_id=payload.version_id,
            submitted_summary=payload.summary,
        )
    except CreatorRoleTemplateError as exc:
        _raise_domain_error(exc)
    if not outcome.review.available:
        return JSONResponse(
            status_code=503,
            content={
                "detail": "creator_role_template_summary_review_unavailable",
                "message": creator_role_template_summary_review_unavailable_display(
                    language
                ),
                "summary_edit_available": True,
            },
        )
    if outcome.review.decision == "reject":
        return JSONResponse(
            status_code=422,
            content={
                "detail": "creator_role_template_summary_rejected",
                "message": creator_role_template_summary_rejection_display(
                    outcome.review.categories,
                    language=language,
                ),
                "reason_categories": list(outcome.review.categories),
                "current_summary": outcome.mutation.version.public_summary,
                "summary_edit_available": False,
            },
        )
    current = _owned_template(principal, template_id)
    return {
        "message": creator_role_template_summary_edit_success_display(language),
        "creator_role_template": _template_response(current, include_versions=True),
    }


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
        raise HTTPException(status_code=400, detail="stats_range_invalid") from exc
    return {
        "status": "ok",
        **get_campaign_funnel(
            campaign_code=template.campaign_code,
            date_from=resolved_from,
            date_to=resolved_to,
        ),
    }


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
            "summary": (
                source.get("published_public_summary")
                or source["published_ai_name"]
            ),
            "expires_at": template.expires_at,
        },
    }


__all__ = ["router"]
