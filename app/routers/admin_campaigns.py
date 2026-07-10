"""Admin campaign codes 路由（/admin/campaign-codes）。

营销活码后台管理：创建/列表/编辑。见 docs/tech_design/campaign_codes_technical_design.md §5.1。
admin + staff 均可读写（Depends(verify_admin_auth)）——运营 staff 应能自主建活码，不必每次找 admin。
本模块不直接引用 settings，故 tests/conftest.py 无需追加 per-module patch。
"""
import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db import (
    create_campaign_code,
    list_campaign_codes,
    update_campaign_code,
)
from app.routers.deps import require_admin_or_staff_user

logger = logging.getLogger("ai4all")
router = APIRouter()

CampaignCodeStatus = Literal["active", "disabled"]


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
