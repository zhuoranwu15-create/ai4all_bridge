"""HTTP composition root；集中组合共享路由与产品路由。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError


async def _product_http_exception_handler(request: Request, exc: HTTPException):
    """按固定路径把 HTTP 错误交给对应产品，非产品路径保持 FastAPI 默认行为。"""

    from app.products.mingchan.api import product_errors as mingchan_errors
    from app.products.zhaoxi.api import product_errors as zhaoxi_errors

    if mingchan_errors.is_public_product_path(request.url.path):
        return await mingchan_errors.localized_http_exception_handler(request, exc)
    if zhaoxi_errors.is_public_product_path(request.url.path):
        return await zhaoxi_errors.localized_http_exception_handler(request, exc)
    return await http_exception_handler(request, exc)


async def _product_validation_exception_handler(
    request: Request, exc: RequestValidationError
):
    """按固定路径把 422 校验错误交给对应产品，避免后注册产品覆盖先注册产品。"""

    from app.products.mingchan.api import product_errors as mingchan_errors
    from app.products.zhaoxi.api import product_errors as zhaoxi_errors

    if mingchan_errors.is_public_product_path(request.url.path):
        return await mingchan_errors.localized_validation_exception_handler(request, exc)
    if zhaoxi_errors.is_public_product_path(request.url.path):
        return await zhaoxi_errors.localized_validation_exception_handler(request, exc)
    return await request_validation_exception_handler(request, exc)


def install_shared_routes(app: FastAPI) -> None:
    """挂载所有部署角色都需要的共享健康检查。"""

    from app.routers import health

    app.include_router(health.router)


def install_central_routes(app: FastAPI) -> None:
    """按既有顺序挂载中心节点控制面与产品路由。"""

    from app.products.plum.manifest import install_public_routes as install_plum_public_routes

    from app.products.mingchan.manifest import (
        install_admin_routes as install_mingchan_admin_routes,
        install_public_routes as install_mingchan_public_routes,
    )
    from app.products.zhaoxi.manifest import (
        install_admin_routes,
        install_operational_routes,
        install_public_routes,
    )
    from app.routers import admin_llm
    from app.routers import admin_ops

    install_public_routes(app)
    install_mingchan_public_routes(app)
    install_plum_public_routes(app)
    install_operational_routes(app)
    app.include_router(admin_ops.router)
    app.include_router(admin_llm.router)
    install_admin_routes(app)
    install_mingchan_admin_routes(app)
    # FastAPI 的异常 handler 是 app 级单例，必须在两个产品路由组合完后统一分派。
    app.add_exception_handler(HTTPException, _product_http_exception_handler)
    app.add_exception_handler(
        RequestValidationError,
        _product_validation_exception_handler,
    )
