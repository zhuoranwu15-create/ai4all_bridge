"""鉴权依赖：bridge / admin / staff / reviewer / web-session。

从 app.main 拆出（结构优化，函数体逐字保留）。各 handler 通过 Depends(...)
引用；settings 在本模块绑定，测试需 patch "app.routers.deps.settings"
（沿用项目 per-module patch 约定）。
"""
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from app.auth_utils import bearer_matches
from app.bootstrap.product_registry import ZHAOXI_APP_ID
from app.config import settings
from app.db import SessionPrincipal, resolve_session_principal, upsert_admin_user


def verify_bridge_auth(authorization: Optional[str] = Header(default=None)) -> None:
    # bearer_matches 内置「空 token 永不通过 + 恒定时间比较」（见 app/auth_utils.py）。
    if not bearer_matches(settings.ai4all_bridge_secret, authorization):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bridge authorization",
        )


def get_admin_user(authorization: Optional[str] = Header(default=None)) -> dict:
    # 依次匹配 admin > reviewer > staff；bearer_matches 对空/未配置 token 一律返回 False，
    # 故空 admin/reviewer/staff token 不会被 "Bearer " 绕过。
    if bearer_matches(settings.admin_token, authorization):
        return upsert_admin_user(
            admin_user_id="admin",
            role="admin",
            display_name="Admin",
        )

    if bearer_matches(getattr(settings, "admin_reviewer_token", ""), authorization):
        return upsert_admin_user(
            admin_user_id="reviewer",
            role="reviewer",
            display_name="Reviewer",
        )

    if bearer_matches(getattr(settings, "admin_staff_token", ""), authorization):
        return upsert_admin_user(
            admin_user_id="staff",
            role="staff",
            display_name="Staff",
        )

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


def _resolve_legacy_session_principal(
    authorization: Optional[str],
) -> Optional[SessionPrincipal]:
    """解析固定 zhaoxi audience 的 legacy Bearer token。"""

    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None
    return resolve_session_principal(
        token=token,
        expected_app_id=ZHAOXI_APP_ID,
    )


def _require_session(
    authorization: Optional[str] = Header(default=None),
) -> SessionPrincipal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    principal = _resolve_legacy_session_principal(authorization)
    if principal is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新验证")
    return principal



__all__ = ['verify_bridge_auth', 'get_admin_user', 'verify_admin_auth', 'require_reviewer_or_admin', 'require_admin_or_staff_user', 'require_admin_user', '_require_session', '_resolve_legacy_session_principal']
