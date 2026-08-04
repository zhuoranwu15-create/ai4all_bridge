"""FastAPI 应用 composition root。"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.bootstrap.http import install_central_routes, install_shared_routes
from app.bootstrap.lifecycle import install_shared_shutdown, install_shared_startup
from app.config import settings
from app.products.zhaoxi.manifest import (
    install_access_routes,
    install_product_lifecycle as install_zhaoxi_lifecycle,
)
from app.products.mingchan.manifest import (
    install_product_lifecycle as install_mingchan_lifecycle,
)

_LOCAL_DEBUG_UI_ENVS = {"local", "development", "test"}
_LOCAL_ONLY_DEBUG_UI_PATHS = {
    "/ui/onboarding_debug.html",
    "/ui/proactive_debug.html",
    "/ui/web_search_debug.html",
}


class ApiPrefixStripMiddleware:
    """对齐 nginx 的 /api 与 /ops 反代前缀，同时保留规范产品 API。"""

    _OPS_API_PREFIXES = ("/admin", "/debug", "/openclaw")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            new_path = None
            if path.startswith("/api/v1/products/"):
                # 这是服务自身的规范产品命名空间，不是前端反代装饰前缀。
                new_path = None
            elif path == "/api" or path.startswith("/api/"):
                new_path = path[4:] or "/"
            elif path.startswith("/ops/"):
                rest = path[4:]
                if any(
                    rest == prefix or rest.startswith(prefix + "/")
                    for prefix in self._OPS_API_PREFIXES
                ):
                    new_path = rest
            if new_path is not None:
                scope = dict(scope)
                scope["path"] = new_path
                raw = scope.get("raw_path")
                if raw:
                    scope["raw_path"] = raw[len(path) - len(new_path) :] or b"/"
        await self.app(scope, receive, send)


def create_app() -> FastAPI:
    """创建并显式组合共享平台与已注册产品。"""

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = FastAPI(title="AI4ALL Weixin Bot", version="0.1.0")

    @app.middleware("http")
    async def gate_debug_ui(request: Request, call_next):
        if (
            request.url.path in _LOCAL_ONLY_DEBUG_UI_PATHS
            and str(settings.app_env or "").lower() not in _LOCAL_DEBUG_UI_ENVS
        ):
            return JSONResponse({"detail": "Not available"}, status_code=403)
        return await call_next(request)

    app.add_middleware(ApiPrefixStripMiddleware)

    install_shared_routes(app)
    install_access_routes(app)

    if settings.has_central_role:
        app.mount("/ui", StaticFiles(directory="app/static", html=True), name="ui")
        app.mount("/ops", StaticFiles(directory="app/static", html=True), name="ops")
        install_central_routes(app)

    if str(settings.app_env or "").lower() in _LOCAL_DEBUG_UI_ENVS:
        app.mount(
            "/user",
            StaticFiles(directory="app/static", html=True),
            name="user_local",
        )

        @app.get("/", include_in_schema=False)
        async def local_root(request: Request) -> RedirectResponse:
            target = "/ui/home.html"
            if request.url.query:
                target = f"{target}?{request.url.query}"
            return RedirectResponse(target)

    install_shared_startup(app)
    install_zhaoxi_lifecycle(app)
    install_mingchan_lifecycle(app)
    install_shared_shutdown(app)
    return app


__all__ = ["ApiPrefixStripMiddleware", "create_app"]
