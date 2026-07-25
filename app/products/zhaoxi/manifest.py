"""朝夕产品的 HTTP 组合清单。"""
from __future__ import annotations

from fastapi import FastAPI

from app.bootstrap.product_registry import ZHAOXI_APP_ID

APP_ID = ZHAOXI_APP_ID


def install_public_routes(app: FastAPI) -> None:
    """按既有顺序挂载朝夕用户侧路由与异常处理器。"""

    from app.products.zhaoxi.api import app_notifications
    from app.products.zhaoxi.api import companion_world
    from app.products.zhaoxi.api import companion_world_human_chat
    from app.products.zhaoxi.api import companion_world_mailbox
    from app.products.zhaoxi.api import companion_world_visits

    app.include_router(companion_world.router)
    companion_world.install_exception_handlers(app)
    app.include_router(companion_world_mailbox.router)
    app.include_router(companion_world_visits.router)
    app.include_router(companion_world_human_chat.router)
    app.include_router(app_notifications.router)


def install_admin_routes(app: FastAPI) -> None:
    """挂载朝夕 Companion World 管理端路由。"""

    from app.products.zhaoxi.api import admin_companion_world

    app.include_router(admin_companion_world.router)
