"""HTTP composition root；集中组合共享路由与产品路由。"""
from __future__ import annotations

from fastapi import FastAPI


def install_shared_routes(app: FastAPI) -> None:
    """挂载所有部署角色都需要的共享健康检查。"""

    from app.routers import health

    app.include_router(health.router)


def install_central_routes(app: FastAPI) -> None:
    """按既有顺序挂载中心节点控制面与朝夕产品路由。"""

    from app.products.zhaoxi.manifest import (
        install_admin_routes,
        install_operational_routes,
        install_public_routes,
    )
    from app.routers import admin_llm
    from app.routers import admin_ops

    install_public_routes(app)
    install_operational_routes(app)
    app.include_router(admin_ops.router)
    app.include_router(admin_llm.router)
    install_admin_routes(app)
