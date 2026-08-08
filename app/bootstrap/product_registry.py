"""服务端可信产品注册表。

生产注册表启用朝夕与鸣蝉。测试可显式构造独立
``ProductRegistry`` 注入数据库原语，但 API 不从 Header 或其他客户端输入动态扩充注册表。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

ZHAOXI_APP_ID = "zhaoxi"
MINGCHAN_APP_ID = "mingchan"
PLUM_APP_ID = "plum"
_APP_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
SUPPORTED_PRODUCT_LANGUAGES = frozenset({"zh-CN", "en-US", "ja-JP"})


@dataclass(frozen=True)
class ProductRegistration:
    """一个可被服务端签发 session、membership 与入口账号的产品。"""

    app_id: str
    enabled: bool = True
    default_language: str = "zh-CN"
    allowed_channels: tuple[str, ...] = ()


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
            default_language = str(product.default_language or "").strip()
            if default_language not in SUPPORTED_PRODUCT_LANGUAGES:
                raise ValueError(
                    f"unsupported default_language for {app_id}: "
                    f"{default_language or '<empty>'}"
                )
            allowed_channels = tuple(
                dict.fromkeys(
                    str(channel or "").strip()
                    for channel in product.allowed_channels
                    if str(channel or "").strip()
                )
            )
            if len(allowed_channels) != len(product.allowed_channels):
                raise ValueError(f"invalid or duplicate allowed_channels for {app_id}")
            entries[app_id] = ProductRegistration(
                app_id=app_id,
                enabled=bool(product.enabled),
                default_language=default_language,
                allowed_channels=allowed_channels,
            )
        if not entries:
            raise ValueError("product registry must not be empty")
        self._products: Mapping[str, ProductRegistration] = MappingProxyType(entries)

    def require_registered(self, app_id: str) -> ProductRegistration:
        """返回已注册产品；未知产品 fail closed，允许调用方识别禁用项。"""

        cleaned = str(app_id or "").strip()
        product = self._products.get(cleaned)
        if product is None:
            raise ValueError(f"unregistered app_id: {cleaned or '<empty>'}")
        return product

    def require_enabled(self, app_id: str) -> ProductRegistration:
        """返回已启用注册项；未知或停用产品一律 fail closed。"""

        product = self.require_registered(app_id)
        if not product.enabled:
            raise ValueError(f"disabled app_id: {product.app_id}")
        return product

    def registrations(self) -> tuple[ProductRegistration, ...]:
        """按 app_id 稳定返回全部服务端注册项。"""

        return tuple(self._products[key] for key in sorted(self._products))

    def require_allowed_channel(
        self,
        app_id: str,
        channel: str,
    ) -> ProductRegistration:
        """要求渠道在产品静态 allowlist 内；不受产品启停状态影响。"""

        product = self.require_registered(app_id)
        cleaned_channel = str(channel or "").strip()
        if cleaned_channel not in product.allowed_channels:
            raise ValueError(
                f"channel not allowed for {product.app_id}: "
                f"{cleaned_channel or '<empty>'}"
            )
        return product


PRODUCTION_PRODUCT_REGISTRY = ProductRegistry(
    [
        ProductRegistration(
            app_id=ZHAOXI_APP_ID,
            default_language="zh-CN",
            allowed_channels=("openclaw-weixin", "web", "unknown"),
        ),
        # 鸣蝉已完成暗部署、schema 63 与朝夕 legacy 资产保留验收。
        ProductRegistration(
            app_id=MINGCHAN_APP_ID,
            default_language="zh-CN",
            allowed_channels=("native",),
        ),
        ProductRegistration(
            app_id=PLUM_APP_ID,
            default_language="zh-CN",
            allowed_channels=("native",),
        ),
    ]
)


def build_test_product_registry(*, mingchan_enabled: bool = True) -> ProductRegistry:
    """构造带隔离测试产品的注册表；可显式验证鸣蝉禁用回滚路径。"""

    return ProductRegistry(
        [
            ProductRegistration(
                app_id=ZHAOXI_APP_ID,
                allowed_channels=("openclaw-weixin", "web", "unknown"),
            ),
            ProductRegistration(
                app_id=MINGCHAN_APP_ID,
                enabled=mingchan_enabled,
                allowed_channels=("native",),
            ),
            ProductRegistration(
                app_id=PLUM_APP_ID,
                allowed_channels=("native",),
            ),
            ProductRegistration(
                app_id="test_product",
                allowed_channels=("native",),
            ),
        ]
    )
