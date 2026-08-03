"""朝夕用户侧 HTTP 错误响应的稳定码与本地化消息。"""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse

from app.bootstrap.product_registry import ZHAOXI_APP_ID
from app.products.zhaoxi.application.product_localization import (
    normalize_public_error_code,
    public_error_message,
)
from app.products.zhaoxi.manifest import (
    CANONICAL_API_PREFIX,
    PROXY_STRIPPED_API_PREFIX,
)

_PUBLIC_PRODUCT_PREFIXES = (
    "/web/",
    "/v1/",
    CANONICAL_API_PREFIX + "/",
    PROXY_STRIPPED_API_PREFIX + "/",
)


def is_public_product_path(path: str) -> bool:
    """判断请求是否属于朝夕用户侧 API；管理与调试端点不在此范围。"""

    value = str(path or "")
    return any(value.startswith(prefix) for prefix in _PUBLIC_PRODUCT_PREFIXES)


async def localized_http_exception_handler(request: Request, exc: HTTPException):
    """公开 API 保留英文 detail 码，并追加产品语言下的用户消息。"""

    if not is_public_product_path(request.url.path):
        return await http_exception_handler(request, exc)
    code = normalize_public_error_code(exc.detail, status_code=exc.status_code)
    response = JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": code,
            "message": public_error_message(
                code,
                status_code=exc.status_code,
                app_id=ZHAOXI_APP_ID,
            ),
        },
        headers=exc.headers,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def install_product_error_handlers(app) -> None:
    """安装仅影响朝夕用户路由的 HTTP 错误本地化处理器。"""

    app.add_exception_handler(HTTPException, localized_http_exception_handler)


__all__ = [
    "install_product_error_handlers",
    "is_public_product_path",
    "localized_http_exception_handler",
]
