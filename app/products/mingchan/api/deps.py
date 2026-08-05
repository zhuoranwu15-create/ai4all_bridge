"""鸣蝉固定产品 audience 的 API 鉴权依赖。"""

from typing import Callable

from fastapi import HTTPException

from app.bootstrap.product_registry import (
    MINGCHAN_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
)
from app.db import SessionPrincipal
from app.routers.deps import require_product_session


def build_mingchan_session_dependency(
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
) -> Callable[..., SessionPrincipal]:
    """创建固定鸣蝉 audience 的 session dependency，允许测试注入注册表。"""

    return require_product_session(MINGCHAN_APP_ID, registry=registry)


def build_mingchan_enabled_dependency(
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
) -> Callable[..., None]:
    """创建请求期鸣蝉启用闸门，确保业务副作用前 fail closed。"""

    registry.require_registered(MINGCHAN_APP_ID)

    def _dependency() -> None:
        try:
            registry.require_enabled(MINGCHAN_APP_ID)
        except ValueError:
            raise HTTPException(status_code=503, detail="产品暂不可用") from None

    _dependency.__name__ = "require_mingchan_enabled"
    return _dependency


_require_session = build_mingchan_session_dependency()


__all__ = [
    "_require_session",
    "build_mingchan_enabled_dependency",
    "build_mingchan_session_dependency",
]
