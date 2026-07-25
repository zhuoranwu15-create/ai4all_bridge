"""HTTP composition root；集中组合共享路由与产品路由。"""
from __future__ import annotations

from fastapi import FastAPI


def install_shared_routes(app: FastAPI) -> None:
    """挂载所有部署角色都需要的健康检查与朝夕接入路由。"""

    from app.products.zhaoxi.api import bridge
    from app.routers import health

    app.include_router(health.router)
    app.include_router(bridge.router)


def install_central_routes(app: FastAPI) -> None:
    """按既有顺序挂载中心节点控制面与朝夕产品路由。"""

    from app.products.zhaoxi.manifest import (
        install_admin_routes,
        install_public_routes,
    )
    from app.routers import admin_llm
    from app.routers import admin_ops
    from app.routers import app_api
    from app.routers import web
    from app.products.zhaoxi.api import admin_accounts
    from app.products.zhaoxi.api import admin_campaigns
    from app.products.zhaoxi.api import admin_dreaming
    from app.products.zhaoxi.api import admin_moderation
    from app.products.zhaoxi.api import admin_proactive
    from app.products.zhaoxi.api import admin_security
    from app.products.zhaoxi.api import debug

    app.include_router(web.router)
    app.include_router(app_api.router)
    install_public_routes(app)
    app.include_router(debug.router)
    app.include_router(admin_moderation.router)
    app.include_router(admin_accounts.router)
    app.include_router(admin_proactive.router)
    app.include_router(admin_dreaming.router)
    app.include_router(admin_ops.router)
    app.include_router(admin_llm.router)
    app.include_router(admin_security.router)
    app.include_router(admin_campaigns.router)
    install_admin_routes(app)
