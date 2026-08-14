"""Plum 产品 HTTP 组合清单。"""
from fastapi import FastAPI

from app.bootstrap.product_registry import PLUM_APP_ID

APP_ID = PLUM_APP_ID
CANONICAL_API_PREFIX = f"/api/v1/products/{APP_ID}"
PROXY_STRIPPED_API_PREFIX = f"/v1/products/{APP_ID}"


def install_public_routes(app: FastAPI) -> None:
    from app.products.plum.api.app import router
    from app.products.plum.api.creation import router as creation_router
    from app.products.plum.api.imports import router as imports_router
    from app.products.plum.api.media import router as media_router

    app.include_router(router, prefix=CANONICAL_API_PREFIX)
    app.include_router(creation_router, prefix=CANONICAL_API_PREFIX)
    app.include_router(imports_router, prefix=CANONICAL_API_PREFIX)
    app.include_router(media_router, prefix=CANONICAL_API_PREFIX)
    app.include_router(
        router,
        prefix=PROXY_STRIPPED_API_PREFIX,
        include_in_schema=False,
    )
    app.include_router(
        creation_router,
        prefix=PROXY_STRIPPED_API_PREFIX,
        include_in_schema=False,
    )
    app.include_router(
        imports_router,
        prefix=PROXY_STRIPPED_API_PREFIX,
        include_in_schema=False,
    )
    app.include_router(
        media_router,
        prefix=PROXY_STRIPPED_API_PREFIX,
        include_in_schema=False,
    )


__all__ = [
    "APP_ID", "CANONICAL_API_PREFIX", "PROXY_STRIPPED_API_PREFIX",
    "install_public_routes",
]
