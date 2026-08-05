"""鸣蝉 Native 登录的固定产品身份编排。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.bootstrap.product_registry import (
    MINGCHAN_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
)
from app.db.accounts import create_platform_user_session
from app.db.billing import register_platform_user_with_referral


def create_mingchan_login_session(
    *,
    phone: str,
    verified_token: str,
    display_name: Optional[str] = None,
    invite_code: Optional[str] = None,
    days: int = 30,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
) -> Dict[str, Any]:
    """消费 OTP，创建/恢复鸣蝉 membership，并签发鸣蝉 audience session。

    本入口不会创建朝夕 membership、微信 owner binding 或旧单 Agent 账号；鸣蝉的首个
    runtime account 由后续 World bootstrap 创建。
    """

    registration = register_platform_user_with_referral(
        phone=phone,
        display_name=display_name,
        invite_code=invite_code,
        verified_token=verified_token,
        app_id=MINGCHAN_APP_ID,
        registry=registry,
    )
    platform_user = registration["platform_user"]
    session = create_platform_user_session(
        platform_user_id=str(platform_user["id"]),
        app_id=MINGCHAN_APP_ID,
        days=days,
        registry=registry,
    )
    return {"registration": registration, "session": session}


__all__ = ["create_mingchan_login_session"]
