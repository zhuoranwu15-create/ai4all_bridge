"""服务端可信产品注册表。

生产注册表当前只启用朝夕。测试可显式构造独立 ``ProductRegistry`` 注入数据库原语，
但 API 不从 Header 或其他客户端输入动态扩充注册表。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

ZHAOXI_APP_ID = "zhaoxi"
_APP_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


@dataclass(frozen=True)
class ProductRegistration:
    """一个可被服务端签发 session、membership 与入口账号的产品。"""

    app_id: str
    enabled: bool = True


class ProductRegistry:
    """不可变产品注册表；只接受启动时/测试代码提供的注册项。"""

    def __init__(self, products: Iterable[ProductRegistration]):
        entries = {}
        for product in products:
            app_id = str(product.app_id or "").strip()
            if not _APP_ID_RE.fullmatch(app_id):
                raise ValueError(f"invalid app_id: {app_id!r}")
            if app_id in entries:
                raise ValueError(f"duplicate app_id: {app_id}")
            entries[app_id] = ProductRegistration(
                app_id=app_id, enabled=bool(product.enabled)
            )
        if not entries:
            raise ValueError("product registry must not be empty")
        self._products: Mapping[str, ProductRegistration] = MappingProxyType(entries)

    def require_enabled(self, app_id: str) -> ProductRegistration:
        """返回已启用注册项；未知或停用产品一律 fail closed。"""

        cleaned = str(app_id or "").strip()
        product = self._products.get(cleaned)
        if product is None:
            raise ValueError(f"unregistered app_id: {cleaned or '<empty>'}")
        if not product.enabled:
            raise ValueError(f"disabled app_id: {cleaned}")
        return product

    def registrations(self) -> tuple[ProductRegistration, ...]:
        """按 app_id 稳定返回全部服务端注册项。"""

        return tuple(self._products[key] for key in sorted(self._products))


PRODUCTION_PRODUCT_REGISTRY = ProductRegistry(
    [ProductRegistration(app_id=ZHAOXI_APP_ID)]
)


def build_test_product_registry() -> ProductRegistry:
    """构造带隔离测试产品的注册表；生产代码不得使用。"""

    return ProductRegistry(
        [
            ProductRegistration(app_id=ZHAOXI_APP_ID),
            ProductRegistration(app_id="test_product"),
        ]
    )
