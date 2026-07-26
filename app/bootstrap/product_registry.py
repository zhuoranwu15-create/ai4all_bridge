"""服务端可信产品注册表。

生产注册表当前只启用朝夕。测试可显式构造独立 ``ProductRegistry`` 注入数据库原语，
但 API 不从 Header 或其他客户端输入动态扩充注册表。

Nooki 受 ``settings.nooki_product_enabled`` 控制：未开启时注册项以 ``enabled=False``
存在，``require_enabled('nooki')`` fail closed；同时 ``app/bootstrap/http.py`` 不挂载
Nooki 路由。朝夕恒启用、不受该开关影响。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from app.config import settings

ZHAOXI_APP_ID = "zhaoxi"
NOOKI_APP_ID = "nooki"
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


def _production_registry() -> ProductRegistry:
    """构造生产注册表；Nooki 的 enabled 绑定 ``settings.nooki_product_enabled``。

    模块导入时读取一次（与 ``settings`` 单例一致），进程生命周期内不再变化；
    测试通过 patch ``app.bootstrap.product_registry.settings`` 或直接构造
    ``ProductRegistry`` 覆盖。
    """

    return ProductRegistry(
        [
            ProductRegistration(app_id=ZHAOXI_APP_ID),
            ProductRegistration(app_id=NOOKI_APP_ID, enabled=settings.nooki_product_enabled),
        ]
    )


PRODUCTION_PRODUCT_REGISTRY = _production_registry()


def build_test_product_registry() -> ProductRegistry:
    """构造带隔离测试产品的注册表；生产代码不得使用。"""

    return ProductRegistry(
        [
            ProductRegistration(app_id=ZHAOXI_APP_ID),
            ProductRegistration(app_id="test_product"),
        ]
    )
