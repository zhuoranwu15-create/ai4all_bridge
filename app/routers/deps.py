"""鉴权依赖：bridge / admin / staff / reviewer / web-session。

从 app.main 拆出（结构优化，函数体逐字保留）。各 handler 通过 Depends(...)
引用；settings 在本模块绑定，测试需 patch "app.routers.deps.settings"
（沿用项目 per-module patch 约定）。
"""
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from app.config import settings
from app.db import get_platform_user_by_session_token, upsert_admin_user


def verify_bridge_auth(authorization: Optional[str] = Header(default=None)) -> None:
    expected = f"Bearer {settings.ai4all_bridge_secret}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bridge authorization",
        )


def get_admin_user(authorization: Optional[str] = Header(default=None)) -> dict:
    admin_expected = f"Bearer {settings.admin_token}"
    if authorization == admin_expected:
        user = upsert_admin_user(
            admin_user_id="admin",
            role="admin",
            display_name="Admin",
        )
        return user

    reviewer_token = str(getattr(settings, "admin_reviewer_token", "") or "").strip()
    if reviewer_token and authorization == f"Bearer {reviewer_token}":
        user = upsert_admin_user(
            admin_user_id="reviewer",
            role="reviewer",
            display_name="Reviewer",
        )
        return user

    staff_token = str(getattr(settings, "admin_staff_token", "") or "").strip()
    if staff_token and authorization == f"Bearer {staff_token}":
        user = upsert_admin_user(
            admin_user_id="staff",
            role="staff",
            display_name="Staff",
        )
        return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid admin authorization",
    )


def verify_admin_auth(admin_user: dict = Depends(get_admin_user)) -> None:
    if admin_user.get("role") not in {"admin", "staff"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin or staff role required",
        )
    return None


def require_reviewer_or_admin(admin_user: dict = Depends(get_admin_user)) -> dict:
    if admin_user.get("role") not in {"admin", "reviewer"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="reviewer or admin role required",
        )
    return admin_user


def require_admin_or_staff_user(admin_user: dict = Depends(get_admin_user)) -> dict:
    if admin_user.get("role") not in {"admin", "staff"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin or staff role required",
        )
    return admin_user


def require_admin_user(admin_user: dict = Depends(get_admin_user)) -> dict:
    if admin_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin role required",
        )
    return admin_user


def _require_session(authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization.removeprefix("Bearer ").strip()
    platform_user = get_platform_user_by_session_token(token=token)
    if platform_user is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新验证")
    return platform_user



__all__ = ['verify_bridge_auth', 'get_admin_user', 'verify_admin_auth', 'require_reviewer_or_admin', 'require_admin_or_staff_user', 'require_admin_user', '_require_session']
