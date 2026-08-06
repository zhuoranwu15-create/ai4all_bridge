"""Fibre 固定测试身份；仅开发环境可用。"""
from fastapi import HTTPException

from app.bootstrap.product_registry import FIBRE_APP_ID, PRODUCTION_PRODUCT_REGISTRY
from app.config import settings
from app.db import SessionPrincipal, connect, require_active_product_membership

_DEV_ENVS = {"local", "development", "test"}


def require_fibre_dev_principal() -> SessionPrincipal:
    """从服务端配置解析固定用户，不接受任何客户端身份输入。"""

    try:
        PRODUCTION_PRODUCT_REGISTRY.require_enabled(FIBRE_APP_ID)
    except ValueError:
        raise HTTPException(status_code=503, detail="fibre_disabled") from None
    env = str(settings.app_env or "").strip().lower()
    if (
        not bool(settings.fibre_enabled)
        or not bool(settings.fibre_dev_mode)
        or env not in _DEV_ENVS
    ):
        raise HTTPException(status_code=403, detail="fibre_dev_identity_disabled")
    platform_user_id = str(settings.fibre_test_user_id).strip()
    with connect() as conn:
        user = conn.execute(
            "SELECT status FROM platform_users WHERE id=?", (platform_user_id,)
        ).fetchone()
    if user is None or str(user["status"]) != "active":
        raise HTTPException(status_code=503, detail="fibre_dev_seed_required")
    try:
        require_active_product_membership(
            platform_user_id=platform_user_id,
            app_id=FIBRE_APP_ID,
            registry=PRODUCTION_PRODUCT_REGISTRY,
        )
    except ValueError:
        raise HTTPException(status_code=503, detail="fibre_dev_seed_required") from None
    return SessionPrincipal(
        session_id="fibre-dev-fixed",
        platform_user_id=platform_user_id,
        app_id=FIBRE_APP_ID,
        expires_at="development-only",
    )


__all__ = ["require_fibre_dev_principal"]
