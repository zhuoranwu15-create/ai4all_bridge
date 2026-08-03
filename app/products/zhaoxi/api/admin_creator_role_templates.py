"""Admin/Staff 用户角色模板查询、聚合统计与处置 API。"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
import json
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.bootstrap.product_registry import PRODUCTION_PRODUCT_REGISTRY
from app.db import get_campaign_funnel
from app.products.zhaoxi.application.campaign_stats import (
    resolve_campaign_stats_range,
)
from app.products.zhaoxi.application.creator_role_template_links import (
    admin_disable_creator_role_template,
    admin_enable_creator_role_template,
    effective_creator_role_template_status,
)
from app.products.zhaoxi.application.creator_role_template_localization import (
    creator_role_template_review_reason_display,
    creator_role_template_review_run_status_display,
    creator_role_template_review_status_display,
    creator_role_template_status_display,
)
from app.products.zhaoxi.domain.creator_role_templates import (
    DISABLED_REASON_MAX_CHARS,
    ZHAOXI_APP_ID,
    CreatorRoleTemplate,
    CreatorRoleTemplateError,
)
from app.products.zhaoxi.infrastructure.persistence.creator_role_templates import (
    get_creator_role_template_for_admin,
    list_creator_role_template_events,
    list_creator_role_template_review_runs,
    list_creator_role_template_summary_review_runs,
    list_creator_role_template_versions,
    list_creator_role_templates_for_admin,
)
from app.routers.deps import require_admin_or_staff_user

router = APIRouter()

TemplateEffectiveStatus = Literal[
    "pending_review",
    "approved",
    "active",
    "rejected",
    "disabled_creator",
    "disabled_admin",
    "expired",
    "deleted",
]
TemplateReviewStatus = Literal["pending", "reviewing", "passed", "rejected"]


class AdminDisableCreatorRoleTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=DISABLED_REASON_MAX_CHARS)


def _raise_domain_error(exc: CreatorRoleTemplateError) -> None:
    if exc.code == "creator_role_template_not_found":
        status_code = 404
    elif exc.code in {
        "creator_role_template_state_conflict",
        "creator_role_template_expired",
    }:
        status_code = 409
    else:
        status_code = 400
    raise HTTPException(status_code=status_code, detail=exc.code) from exc


def _admin_template(template_id: str) -> CreatorRoleTemplate:
    """只按服务端固定产品和模板 ID 定位 owner，避免信任查询参数中的 creator。"""
    template = get_creator_role_template_for_admin(
        app_id=ZHAOXI_APP_ID,
        template_id=template_id,
        include_deleted=True,
    )
    if template is None:
        raise HTTPException(status_code=404, detail="creator_role_template_not_found")
    return template


def _version_dict(version, *, language: str) -> Dict[str, Any]:
    value = asdict(version)
    value["is_published"] = bool(value["is_published"])
    value["review_status_display"] = creator_role_template_review_status_display(
        value["review_status"], language
    )
    value["review_reason_display"] = creator_role_template_review_reason_display(
        value["review_categories_json"],
        review_status=value["review_status"],
        language=language,
    )
    return value


def _safe_event_dict(event: Dict[str, Any]) -> Dict[str, Any]:
    """移除审计元数据中的被邀请账号 ID，Staff 只需聚合漏斗而非用户明细。"""
    value = dict(event)
    try:
        metadata = json.loads(str(value.get("metadata_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    if isinstance(metadata, dict):
        metadata.pop("account_id", None)
    else:
        metadata = {}
    value["metadata_json"] = json.dumps(
        metadata,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return value


def _template_dict(
    template: CreatorRoleTemplate,
    *,
    include_versions: bool,
) -> Dict[str, Any]:
    versions = list_creator_role_template_versions(
        creator_platform_user_id=template.creator_platform_user_id,
        app_id=template.app_id,
        template_id=template.id,
        include_deleted_template=True,
    )
    latest = versions[0] if versions else None
    published = next((version for version in versions if version.is_published), None)
    language = PRODUCTION_PRODUCT_REGISTRY.require_enabled(
        template.app_id
    ).default_language
    effective_status = effective_creator_role_template_status(template)
    result: Dict[str, Any] = {
        **asdict(template),
        "status_display": creator_role_template_status_display(
            template.status, language
        ),
        "effective_status": effective_status,
        "effective_status_display": creator_role_template_status_display(
            effective_status, language
        ),
        "latest_version": _version_dict(latest, language=language) if latest else None,
        "published_version": (
            _version_dict(published, language=language) if published else None
        ),
    }
    if include_versions:
        result["versions"] = [
            _version_dict(version, language=language) for version in versions
        ]
    return result


def _created_range(
    date_from: Optional[str], date_to: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """把可选北京自然日筛选转换为左闭右开数据库时间范围。"""
    if date_from is None and date_to is None:
        return None, None
    try:
        start = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else None
        end = datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=400, detail="from must be <= to")
    return (
        start.strftime("%Y-%m-%d 00:00:00") if start is not None else None,
        (end + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
        if end is not None
        else None,
    )


@router.get("/admin/creator-role-templates")
def admin_list_creator_role_templates(
    creator: Optional[str] = None,
    status: Optional[TemplateEffectiveStatus] = None,
    review_status: Optional[TemplateReviewStatus] = None,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _: dict = Depends(require_admin_or_staff_user),
) -> dict:
    """分页查询朝夕用户角色模板，可按 owner、有效状态、审核状态和创建日筛选。"""
    created_from, created_to_exclusive = _created_range(date_from, date_to)
    templates, total = list_creator_role_templates_for_admin(
        app_id=ZHAOXI_APP_ID,
        creator_platform_user_id=creator,
        effective_status=status,
        review_status=review_status,
        created_from=created_from,
        created_to_exclusive=created_to_exclusive,
        limit=limit,
        offset=offset,
    )
    return {
        "creator_role_templates": [
            _template_dict(template, include_versions=False)
            for template in templates
        ],
        "pagination": {"limit": limit, "offset": offset, "total": total},
    }


@router.get("/admin/creator-role-templates/{template_id}")
def admin_creator_role_template_detail(
    template_id: str,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    _: dict = Depends(require_admin_or_staff_user),
) -> dict:
    """返回模板版本、LLM review runs、事件和与运营活码同口径的聚合漏斗。"""
    template = _admin_template(template_id)
    try:
        resolved_from, resolved_to = resolve_campaign_stats_range(date_from, date_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    template_data = _template_dict(template, include_versions=True)
    language = PRODUCTION_PRODUCT_REGISTRY.require_enabled(
        template.app_id
    ).default_language
    review_runs = []
    summary_review_runs = []
    for version in template_data["versions"]:
        for review_run in list_creator_role_template_review_runs(
            creator_platform_user_id=template.creator_platform_user_id,
            app_id=template.app_id,
            template_id=template.id,
            version_id=version["id"],
        ):
            localized_run = dict(review_run)
            localized_run["status_display"] = (
                creator_role_template_review_run_status_display(
                    localized_run["status"], language
                )
            )
            localized_run["reason_display"] = (
                creator_role_template_review_reason_display(
                    localized_run.get("categories_json"),
                    review_status=(
                        "rejected"
                        if localized_run.get("status") == "rejected"
                        else ""
                    ),
                    language=language,
                )
            )
            review_runs.append(localized_run)
        for summary_run in list_creator_role_template_summary_review_runs(
            creator_platform_user_id=template.creator_platform_user_id,
            app_id=template.app_id,
            template_id=template.id,
            version_id=version["id"],
        ):
            localized_summary_run = dict(summary_run)
            localized_summary_run["status_display"] = (
                creator_role_template_review_run_status_display(
                    localized_summary_run["status"], language
                )
            )
            localized_summary_run["reason_display"] = (
                creator_role_template_review_reason_display(
                    localized_summary_run.get("categories_json"),
                    review_status=(
                        "rejected"
                        if localized_summary_run.get("status") == "rejected"
                        else ""
                    ),
                    language=language,
                )
            )
            summary_review_runs.append(localized_summary_run)
    events = [
        _safe_event_dict(event)
        for event in list_creator_role_template_events(
            creator_platform_user_id=template.creator_platform_user_id,
            app_id=template.app_id,
            template_id=template.id,
            include_deleted_template=True,
            limit=500,
        )
    ]
    return {
        "creator_role_template": template_data,
        "review_runs": review_runs,
        "summary_review_runs": summary_review_runs,
        "events": events,
        "stats": get_campaign_funnel(
            campaign_code=template.campaign_code,
            date_from=resolved_from,
            date_to=resolved_to,
        ),
    }


@router.post("/admin/creator-role-templates/{template_id}/disable")
def admin_disable_creator_role_template_link(
    template_id: str,
    payload: AdminDisableCreatorRoleTemplateRequest,
    admin_user: dict = Depends(require_admin_or_staff_user),
) -> dict:
    """Admin/Staff 必须填写原因后停用模板，并由 persistence 记录 actor 和前状态。"""
    template = _admin_template(template_id)
    try:
        updated = admin_disable_creator_role_template(
            creator_platform_user_id=template.creator_platform_user_id,
            app_id=template.app_id,
            template_id=template.id,
            admin_user_id=str(admin_user["id"]),
            reason=payload.reason,
        )
    except CreatorRoleTemplateError as exc:
        _raise_domain_error(exc)
    return {"creator_role_template": _template_dict(updated, include_versions=False)}


@router.post("/admin/creator-role-templates/{template_id}/enable")
def admin_enable_creator_role_template_link(
    template_id: str,
    admin_user: dict = Depends(require_admin_or_staff_user),
) -> dict:
    """Admin/Staff 恢复仍在有效期的模板到平台停用前状态。"""
    template = _admin_template(template_id)
    try:
        updated = admin_enable_creator_role_template(
            creator_platform_user_id=template.creator_platform_user_id,
            app_id=template.app_id,
            template_id=template.id,
            admin_user_id=str(admin_user["id"]),
        )
    except CreatorRoleTemplateError as exc:
        _raise_domain_error(exc)
    return {"creator_role_template": _template_dict(updated, include_versions=False)}


__all__ = ["router"]
