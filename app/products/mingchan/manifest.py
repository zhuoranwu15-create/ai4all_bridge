"""鸣蝉产品的 HTTP 组合清单骨架。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI

from app.bootstrap.product_registry import (
    MINGCHAN_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
)

APP_ID = MINGCHAN_APP_ID
CANONICAL_API_PREFIX = f"/api/v1/products/{APP_ID}"
PROXY_STRIPPED_API_PREFIX = f"/v1/products/{APP_ID}"

_REPLACED_WORLD_ROUTES = frozenset(
    {
        ("/worlds/home/bootstrap", "POST"),
        ("/worlds/home/resident-candidates", "GET"),
        ("/worlds/home/residents/confirm", "POST"),
        ("/worlds/home/residents", "GET"),
        ("/conversations", "GET"),
        ("/ai-conversations/{conversation_id}/messages", "GET"),
        ("/ai-conversations/{conversation_id}/read", "POST"),
        ("/ai-conversations/{conversation_id}/turn", "POST"),
    }
)


def _without_replaced_world_routes(router: APIRouter) -> APIRouter:
    """过滤已由鸣蝉新垂直切片接管的旧 World 路由，避免重复注册。"""

    filtered = APIRouter()
    filtered.routes.extend(
        route
        for route in router.routes
        if not any(
            (getattr(route, "path", None), method) in _REPLACED_WORLD_ROUTES
            for method in (getattr(route, "methods", None) or ())
        )
    )
    return filtered


def install_product_lifecycle(app: FastAPI) -> None:
    """通过 manifest 暴露鸣蝉 lifecycle；disabled 时不会启动产品任务。"""

    from app.products.mingchan.lifecycle import install_lifecycle

    install_lifecycle(app)


def install_public_routes(
    app: FastAPI,
    *,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
    config=None,
) -> None:
    """仅在鸣蝉固定 namespace 下挂载 Native 身份路由，支持测试配置注入。"""

    from app.products.mingchan.api.auth import build_router
    from app.products.mingchan.api.app import build_router as build_app_router
    from app.products.mingchan.api.conversations import (
        build_router as build_conversation_router,
    )
    from app.products.mingchan.api.notifications import (
        build_router as build_notification_router,
    )
    from app.products.mingchan.api.world_onboarding import (
        build_router as build_world_onboarding_router,
    )
    from app.products.mingchan.api import human_chat, mailbox, media, resident_wishes, visits, world
    from app.products.mingchan.api.deps import build_mingchan_enabled_dependency

    identity_router = build_router(registry, config=config)
    app_router = build_app_router(registry, config=config)
    conversation_router = build_conversation_router(registry, config=config)
    notification_router = build_notification_router(registry, config=config)
    world_onboarding_router = build_world_onboarding_router(
        registry,
        config=config,
    )
    world.configure_runtime(registry, config=config)
    mailbox.configure_runtime(registry)
    extended_routers = (
        _without_replaced_world_routes(world.router),
        resident_wishes.router,
        mailbox.router,
        visits.router,
        human_chat.router,
        media.router,
    )
    require_enabled = build_mingchan_enabled_dependency(registry)
    app.include_router(identity_router, prefix=CANONICAL_API_PREFIX)
    app.include_router(app_router, prefix=CANONICAL_API_PREFIX)
    app.include_router(conversation_router, prefix=CANONICAL_API_PREFIX)
    app.include_router(notification_router, prefix=CANONICAL_API_PREFIX)
    app.include_router(world_onboarding_router, prefix=CANONICAL_API_PREFIX)
    for extended_router in extended_routers:
        app.include_router(
            extended_router,
            prefix=CANONICAL_API_PREFIX,
            dependencies=[Depends(require_enabled)],
        )
    app.include_router(
        identity_router,
        prefix=PROXY_STRIPPED_API_PREFIX,
        include_in_schema=False,
    )
    app.include_router(
        app_router,
        prefix=PROXY_STRIPPED_API_PREFIX,
        include_in_schema=False,
    )
    app.include_router(
        conversation_router,
        prefix=PROXY_STRIPPED_API_PREFIX,
        include_in_schema=False,
    )
    app.include_router(
        notification_router,
        prefix=PROXY_STRIPPED_API_PREFIX,
        include_in_schema=False,
    )
    app.include_router(
        world_onboarding_router,
        prefix=PROXY_STRIPPED_API_PREFIX,
        include_in_schema=False,
    )
    for extended_router in extended_routers:
        app.include_router(
            extended_router,
            prefix=PROXY_STRIPPED_API_PREFIX,
            dependencies=[Depends(require_enabled)],
            include_in_schema=False,
        )
    world.install_exception_handlers(app)
    from app.products.mingchan.api import world_onboarding

    world_onboarding.install_exception_handlers(app)


def install_admin_routes(app: FastAPI) -> None:
    """挂载鸣蝉 World 管理端路由；产品禁用时仍允许上线前配置。"""

    from app.products.mingchan.api import admin_world

    app.include_router(admin_world.router)


__all__ = [
    "APP_ID",
    "CANONICAL_API_PREFIX",
    "PROXY_STRIPPED_API_PREFIX",
    "install_public_routes",
    "install_admin_routes",
    "install_product_lifecycle",
]
