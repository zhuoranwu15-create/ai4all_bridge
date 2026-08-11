"""Plum 产品身份编排；外部 provider 验证完成后从这里进入。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.bootstrap.product_registry import (
    PLUM_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
)
from app.db import create_platform_user_session, get_platform_user
from app.products.plum.infrastructure.repository import ensure_plum_user


def create_plum_login_session(
    *,
    verified_platform_user_id: str,
    display_name: Optional[str] = None,
    days: int = 30,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
) -> Dict[str, Any]:
    """Provision Plum for an already verified User and issue a Plum session.

    The caller must resolve and verify the external identity before invoking this
    function. Keeping provider credentials outside this boundary lets email,
    Google, Apple or another adapter share the same product provisioning path.
    """

    platform_user_id = str(verified_platform_user_id or "").strip()
    if not platform_user_id:
        raise ValueError("verified_platform_user_id_required")
    platform_user = get_platform_user(platform_user_id=platform_user_id)
    if platform_user is None or str(platform_user.get("status")) != "active":
        raise ValueError("platform_user_not_active")
    resolved_name = (
        str(display_name or "").strip()
        or str(platform_user.get("display_name") or "").strip()
        or "Plum User"
    )
    provisioning = ensure_plum_user(
        platform_user_id=platform_user_id,
        display_name=resolved_name,
        registry=registry,
    )
    session = create_platform_user_session(
        platform_user_id=platform_user_id,
        app_id=PLUM_APP_ID,
        days=max(1, int(days)),
        registry=registry,
    )
    return {
        "platform_user": get_platform_user(platform_user_id=platform_user_id),
        "is_new_membership": bool(provisioning["is_new_membership"]),
        "provisioning": provisioning,
        "session": session,
    }


__all__ = ["create_plum_login_session"]
