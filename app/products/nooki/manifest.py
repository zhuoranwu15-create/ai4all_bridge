"""Nooki 产品的 HTTP 组合清单。

P0+P1 范围内只有用户侧路由（登录、聊天、状态、按钮态任务操作），没有 lifecycle/admin/operational
入口——没有 dreaming/proactive/moderation 等朝夕专属能力，故只暴露 `install_public_routes`。
按 `docs/guides/adding-product.md`：新产品不复制朝夕的 legacy `/v1` 挂载，只用规范 + 反代剥前缀。
"""
from __future__ import annotations

from fastapi import FastAPI

from app.bootstrap.product_registry import NOOKI_APP_ID

APP_ID = NOOKI_APP_ID
CANONICAL_API_PREFIX = f"/api/v1/products/{APP_ID}"
PROXY_STRIPPED_API_PREFIX = f"/v1/products/{APP_ID}"


def _install_versioned_product_router(app: FastAPI, router) -> None:
    """把同一个产品 router 挂到规范和反代剥前缀两条固定路径（不带朝夕的 legacy `/v1`）。"""

    app.include_router(router, prefix=CANONICAL_API_PREFIX)
    app.include_router(
        router,
        prefix=PROXY_STRIPPED_API_PREFIX,
        include_in_schema=False,
    )


def install_public_routes(app: FastAPI) -> None:
    """挂载 Nooki 用户侧路由：登录、人设/聊天/状态、按钮态任务操作。"""

    from app.products.nooki.api import app as app_api
    from app.products.nooki.api import auth
    from app.products.nooki.api import tasks

    for product_router in (auth.router, app_api.router, tasks.router):
        _install_versioned_product_router(app, product_router)


__all__ = [
    "APP_ID",
    "CANONICAL_API_PREFIX",
    "PROXY_STRIPPED_API_PREFIX",
    "install_public_routes",
]
