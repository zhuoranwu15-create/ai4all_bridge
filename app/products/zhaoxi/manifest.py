"""朝夕产品的 HTTP 组合清单。"""
from __future__ import annotations

from fastapi import FastAPI

from app.bootstrap.product_registry import ZHAOXI_APP_ID

APP_ID = ZHAOXI_APP_ID
CANONICAL_API_PREFIX = f"/api/v1/products/{APP_ID}"
PROXY_STRIPPED_API_PREFIX = f"/v1/products/{APP_ID}"


def install_product_lifecycle(app: FastAPI) -> None:
    """通过 manifest 暴露朝夕产品生命周期。"""

    from app.products.zhaoxi.lifecycle import install_lifecycle

    install_lifecycle(app)


def install_access_routes(app: FastAPI) -> None:
    """挂载所有部署角色都需要的朝夕 OpenClaw/节点入口。"""

    from app.products.zhaoxi.api import bridge

    app.include_router(bridge.router)


def install_public_routes(app: FastAPI) -> None:
    """按既有顺序挂载朝夕用户侧路由与异常处理器。"""

    from app.products.zhaoxi.api import creator_role_templates
    from app.products.zhaoxi.api import product_errors

    from app.routers import web

    app.include_router(web.router)
    app.include_router(creator_role_templates.router)
    product_errors.install_product_error_handlers(app)


def install_operational_routes(app: FastAPI) -> None:
    """挂载需先于共享 ops 路由注册的朝夕运营与调试入口。"""

    from app.products.zhaoxi.api import admin_accounts
    from app.products.zhaoxi.api import admin_dreaming
    from app.products.zhaoxi.api import admin_moderation
    from app.products.zhaoxi.api import admin_proactive
    from app.products.zhaoxi.api import debug

    app.include_router(debug.router)
    app.include_router(admin_moderation.router)
    app.include_router(admin_accounts.router)
    app.include_router(admin_proactive.router)
    app.include_router(admin_dreaming.router)


def install_admin_routes(app: FastAPI) -> None:
    """挂载朝夕安全、活动和角色模板管理端路由。"""

    from app.products.zhaoxi.api import admin_campaigns
    from app.products.zhaoxi.api import admin_creator_role_templates
    from app.products.zhaoxi.api import admin_security

    app.include_router(admin_security.router)
    app.include_router(admin_campaigns.router)
    app.include_router(admin_creator_role_templates.router)


__all__ = [
    "APP_ID",
    "CANONICAL_API_PREFIX",
    "PROXY_STRIPPED_API_PREFIX",
    "install_access_routes",
    "install_admin_routes",
    "install_operational_routes",
    "install_product_lifecycle",
    "install_public_routes",
]
