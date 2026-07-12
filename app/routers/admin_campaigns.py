"""Admin campaign codes 路由（/admin/campaign-codes）。

营销活码后台管理：创建/列表/编辑。见 docs/tech_design/campaign_codes_technical_design.md §5.1。
admin + staff 均可读写（Depends(verify_admin_auth)）——运营 staff 应能自主建活码，不必每次找 admin。
本模块不直接引用 settings，故 tests/conftest.py 无需追加 per-module patch。
"""
import logging
from datetime import datetime, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.db import (
    create_campaign_code,
    get_campaign_funnel,
    list_campaign_codes,
    update_campaign_code,
)
from app.routers.deps import require_admin_or_staff_user
from app.time_utils import beijing_now

logger = logging.getLogger("ai4all")
router = APIRouter()

CampaignCodeStatus = Literal["active", "disabled"]

# 漏斗统计查询窗口约束（北京自然日）。
_DEFAULT_STATS_WINDOW_DAYS = 14
_MAX_STATS_WINDOW_DAYS = 92


class CampaignCodeCreateRequest(BaseModel):
    code: str
    campaign_key: str
    status: CampaignCodeStatus = "active"
    valid_from: Optional[str] = None
    expires_at: Optional[str] = None
    mission_id: Optional[str] = None
    onboarding_script_variant: Optional[str] = None
    soul_preset_key: Optional[str] = None
    ai_name_preset: Optional[str] = None


class CampaignCodeUpdateRequest(BaseModel):
    campaign_key: Optional[str] = None
    status: Optional[CampaignCodeStatus] = None
    valid_from: Optional[str] = None
    expires_at: Optional[str] = None
    mission_id: Optional[str] = None
    onboarding_script_variant: Optional[str] = None
    soul_preset_key: Optional[str] = None
    ai_name_preset: Optional[str] = None


@router.post("/admin/campaign-codes")
def admin_create_campaign_code(
    payload: CampaignCodeCreateRequest,
    admin_user: dict = Depends(require_admin_or_staff_user),
) -> dict:
    try:
        campaign = create_campaign_code(
            code=payload.code,
            campaign_key=payload.campaign_key,
            status=payload.status,
            valid_from=payload.valid_from,
            expires_at=payload.expires_at,
            mission_id=payload.mission_id,
            onboarding_script_variant=payload.onboarding_script_variant,
            soul_preset_key=payload.soul_preset_key,
            ai_name_preset=payload.ai_name_preset,
            created_by_admin_user_id=str(admin_user["id"]),
        )
    except ValueError as err:
        detail = str(err)
        status_code = 409 if "already exists" in detail else 400
        raise HTTPException(status_code=status_code, detail=detail)
    return {"status": "ok", "campaign_code": campaign}


@router.get("/admin/campaign-codes")
def admin_list_campaign_codes(
    status: Optional[str] = None,
    limit: int = 100,
    _: dict = Depends(require_admin_or_staff_user),
) -> dict:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    return {"campaign_codes": list_campaign_codes(status=status, limit=limit)}


@router.patch("/admin/campaign-codes/{code}")
def admin_update_campaign_code(
    code: str,
    payload: CampaignCodeUpdateRequest,
    _: dict = Depends(require_admin_or_staff_user),
) -> dict:
    fields = payload.model_dump(exclude_unset=True)
    try:
        campaign = update_campaign_code(code=code, **fields)
    except ValueError as err:
        detail = str(err)
        status_code = 404 if "not found" in detail else 400
        raise HTTPException(status_code=status_code, detail=detail)
    return {"status": "ok", "campaign_code": campaign}


def _resolve_stats_range(date_from: Optional[str], date_to: Optional[str]) -> tuple[str, str]:
    """解析/校验统计日期区间（北京自然日）；缺省给最近 14 天，上限 92 天。"""
    today = beijing_now().date()

    def _parse(value: Optional[str], default):
        if value is None:
            return default
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    to_d = _parse(date_to, today)
    from_d = _parse(date_from, to_d - timedelta(days=_DEFAULT_STATS_WINDOW_DAYS - 1))
    if from_d > to_d:
        raise HTTPException(status_code=400, detail="from must be <= to")
    if (to_d - from_d).days > _MAX_STATS_WINDOW_DAYS:
        raise HTTPException(
            status_code=400, detail=f"range must be <= {_MAX_STATS_WINDOW_DAYS} days"
        )
    return from_d.isoformat(), to_d.isoformat()


@router.get("/admin/campaign-codes/{code}/stats")
def admin_campaign_code_stats(
    code: str,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    _: dict = Depends(require_admin_or_staff_user),
) -> dict:
    """campaign 漏斗近实时统计（S0 曝光 → S5 onboarding 完成）。

    查询时对既有表现算聚合（get_campaign_funnel 内含 S0 曝光），返回 totals/rates/by_day。
    见 campaign_funnel_analytics_technical_design.md §7.5。
    """
    resolved_from, resolved_to = _resolve_stats_range(date_from, date_to)
    funnel = get_campaign_funnel(
        campaign_code=code, date_from=resolved_from, date_to=resolved_to
    )
    return {"status": "ok", **funnel}