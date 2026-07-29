"""HTTP composition root；集中组合共享路由与产品路由。"""
from __future__ import annotations

from fastapi import FastAPI

from app.config import settings


def install_shared_routes(app: FastAPI) -> None:
    """挂载所有部署角色都需要的共享健康检查。"""

    from app.routers import agent, health

    app.include_router(health.router)
    app.include_router(agent.router)


def install_central_routes(app: FastAPI) -> None:
    """按既有顺序挂载中心节点控制面、朝夕产品路由与 Nooki 产品路由。

    Nooki 路由只在 ``settings.nooki_product_enabled`` 开启时挂载；未开启时不 import
    Nooki 模块、不注册任何 Nooki 路由，避免半启动形态。朝夕路由不受该开关影响。
    """

    from app.products.zhaoxi.manifest import (
        install_admin_routes,
        install_operational_routes,
        install_public_routes,
    )
    from app.routers import admin_llm
    from app.routers import admin_ops

    install_public_routes(app)
    if settings.nooki_product_enabled:
        from app.products.nooki.manifest import install_public_routes as install_nooki_public_routes

        install_nooki_public_routes(app)
    install_operational_routes(app)
    app.include_router(admin_ops.router)
    app.include_router(admin_llm.router)
    install_admin_routes(app)
