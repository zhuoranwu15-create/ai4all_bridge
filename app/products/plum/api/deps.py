"""Plum Web member/Guest identity guards with a local fixed-user fallback."""
import hmac
from typing import Optional, Union

from fastapi import HTTPException, Request

from app.bootstrap.product_registry import PLUM_APP_ID, PRODUCTION_PRODUCT_REGISTRY
from app.config import settings
from app.db import (
    SessionPrincipal,
    connect,
    require_active_product_membership,
    resolve_session_principal,
)
from app.products.plum.infrastructure.guest_repository import (
    GuestPrincipal,
    guest_csrf_matches,
    resolve_guest_principal,
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


def require_plum_member(request: Request) -> SessionPrincipal:
    """Explicit Member-only guard; retained alias keeps existing routes stable."""

    return require_plum_principal(request)


PlumActorPrincipal = Union[SessionPrincipal, GuestPrincipal]


def optional_plum_actor(request: Request) -> Optional[PlumActorPrincipal]:
    """Resolve a valid Member first, then a Guest, without rejecting visitors."""

    require_plum_available()
    member_token = request.cookies.get(str(settings.plum_session_cookie_name)) or ""
    if member_token:
        member = resolve_session_principal(
            token=member_token,
            expected_app_id=PLUM_APP_ID,
            registry=PRODUCTION_PRODUCT_REGISTRY,
        )
        if member is not None:
            return member
    guest_token = request.cookies.get(str(settings.plum_guest_session_cookie_name)) or ""
    if bool(settings.plum_guest_chat_enabled) and guest_token:
        guest = resolve_guest_principal(token=guest_token)
        if guest is not None:
            return guest
    env = str(settings.app_env or "").strip().lower()
    if bool(settings.plum_dev_mode) and env in _DEV_ENVS:
        try:
            return require_plum_principal(request)
        except HTTPException:
            return None
    return None


def require_plum_actor(request: Request) -> PlumActorPrincipal:
    actor = optional_plum_actor(request)
    if actor is None:
        raise HTTPException(status_code=401, detail="authentication_required")
    if isinstance(actor, GuestPrincipal):
        cookie = request.cookies.get(str(settings.plum_csrf_cookie_name)) or ""
        header = request.headers.get("X-Plum-CSRF") or ""
        if (
            request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
            and (
                not cookie
                or not hmac.compare_digest(cookie, header)
                or not guest_csrf_matches(
                    session_id=actor.guest_session_id, csrf_token=cookie
                )
            )
        ):
            raise HTTPException(status_code=403, detail="csrf_validation_failed")
    else:
        # Cookie-backed Member sessions require CSRF; the local fixed seed has
        # no browser session and keeps the existing development-only behavior.
        if request.cookies.get(str(settings.plum_session_cookie_name)):
            _require_csrf(request)
    return actor


def plum_session_token(request: Request) -> str:
    return str(request.cookies.get(str(settings.plum_session_cookie_name)) or "")


__all__ = [
    "GuestPrincipal",
    "PlumActorPrincipal",
    "optional_plum_actor",
    "plum_session_token",
    "require_plum_available",
    "require_plum_actor",
    "require_plum_member",
    "require_plum_principal",
]
