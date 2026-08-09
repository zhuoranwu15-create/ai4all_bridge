"""Plum Web session identity with a local fixed-user fallback."""
import hmac

from fastapi import HTTPException, Request

from app.bootstrap.product_registry import PLUM_APP_ID, PRODUCTION_PRODUCT_REGISTRY
from app.config import settings
from app.db import (
    SessionPrincipal,
    connect,
    require_active_product_membership,
    resolve_session_principal,
)

_DEV_ENVS = {"local", "development", "test"}


def require_plum_available() -> None:
    """Reject Plum routes when the product is disabled by configuration."""

    if not bool(settings.plum_enabled):
        raise HTTPException(status_code=503, detail="plum_disabled")
    try:
        PRODUCTION_PRODUCT_REGISTRY.require_enabled(PLUM_APP_ID)
    except ValueError:
        raise HTTPException(status_code=503, detail="plum_disabled") from None


def _require_csrf(request: Request) -> None:
    if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return
    cookie = request.cookies.get(str(settings.plum_csrf_cookie_name)) or ""
    header = request.headers.get("X-Plum-CSRF") or ""
    if not cookie or not hmac.compare_digest(cookie, header):
        raise HTTPException(status_code=403, detail="csrf_validation_failed")


def require_plum_principal(request: Request) -> SessionPrincipal:
    """Resolve a product-scoped cookie session; local dev may use the fixed seed."""

    require_plum_available()
    token = request.cookies.get(str(settings.plum_session_cookie_name)) or ""
    if token and bool(settings.plum_public_test_auth_enabled):
        principal = resolve_session_principal(
            token=token,
            expected_app_id=PLUM_APP_ID,
            registry=PRODUCTION_PRODUCT_REGISTRY,
        )
        if principal is None:
            raise HTTPException(status_code=401, detail="session_expired")
        _require_csrf(request)
        return principal

    env = str(settings.app_env or "").strip().lower()
    if (
        not bool(settings.plum_dev_mode)
        or env not in _DEV_ENVS
    ):
        raise HTTPException(status_code=401, detail="authentication_required")
    platform_user_id = str(settings.plum_test_user_id).strip()
    with connect() as conn:
        user = conn.execute(
            "SELECT status FROM platform_users WHERE id=?", (platform_user_id,)
        ).fetchone()
    if user is None or str(user["status"]) != "active":
        raise HTTPException(status_code=503, detail="plum_dev_seed_required")
    try:
        require_active_product_membership(
            platform_user_id=platform_user_id,
            app_id=PLUM_APP_ID,
            registry=PRODUCTION_PRODUCT_REGISTRY,
        )
    except ValueError:
        raise HTTPException(status_code=503, detail="plum_dev_seed_required") from None
    return SessionPrincipal(
        session_id="plum-dev-fixed",
        platform_user_id=platform_user_id,
        app_id=PLUM_APP_ID,
        expires_at="development-only",
    )


def plum_session_token(request: Request) -> str:
    return str(request.cookies.get(str(settings.plum_session_cookie_name)) or "")


__all__ = [
    "plum_session_token",
    "require_plum_available",
    "require_plum_principal",
]
