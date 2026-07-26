"""Nooki 生产开关：关闭时路由不挂载、registry fail closed；朝夕恒启用不受影响。

覆盖要求（来自阶段1 完成标准）：
- 开关 false → Nooki 路由不挂载 → require_enabled("nooki") 失败
- 开关 true  → Nooki 路由正常挂载 → require_enabled("nooki") 成功
- 无论开关真假 → zhaoxi 正常
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI

from app.bootstrap import http as http_module
from app.bootstrap.product_registry import (
    NOOKI_APP_ID,
    ZHAOXI_APP_ID,
    ProductRegistry,
    ProductRegistration,
)


def _routes_containing(app, needle: str) -> set[str]:
    """收集 app.routes 里 path 含 needle 的路径集合。"""

    return {
        route.path
        for route in getattr(app, "routes", [])
        if hasattr(route, "path") and needle in route.path
    }


# ---------------------------------------------------------------------------
# Registry 层：enabled 直接决定 require_enabled 行为
# ---------------------------------------------------------------------------


def test_registry_rejects_nooki_when_disabled():
    """开关 false 时 require_enabled('nooki') 抛 ValueError，fail closed。"""

    registry = ProductRegistry(
        [
            ProductRegistration(app_id=ZHAOXI_APP_ID),
            ProductRegistration(app_id=NOOKI_APP_ID, enabled=False),
        ]
    )
    # zhaoxi 恒启用
    assert registry.require_enabled(ZHAOXI_APP_ID).app_id == ZHAOXI_APP_ID
    # nooki 被拒
    with pytest.raises(ValueError, match="disabled app_id"):
        registry.require_enabled(NOOKI_APP_ID)


def test_registry_accepts_nooki_when_enabled():
    """开关 true 时 require_enabled('nooki') 正常返回注册项。"""

    registry = ProductRegistry(
        [
            ProductRegistration(app_id=ZHAOXI_APP_ID),
            ProductRegistration(app_id=NOOKI_APP_ID, enabled=True),
        ]
    )
    assert registry.require_enabled(NOOKI_APP_ID).app_id == NOOKI_APP_ID
    assert registry.require_enabled(ZHAOXI_APP_ID).app_id == ZHAOXI_APP_ID


# ---------------------------------------------------------------------------
# HTTP 挂载层：install_central_routes 按开关决定是否挂 Nooki 路由
# ---------------------------------------------------------------------------


def test_central_routes_skip_nooki_when_flag_off(monkeypatch):
    """开关 false 时 install_central_routes 不挂载任何 Nooki 路由。"""

    monkeypatch.setattr(http_module.settings, "nooki_product_enabled", False)
    app = FastAPI()
    http_module.install_central_routes(app)

    assert _routes_containing(app, "/products/nooki") == set()


def test_central_routes_mount_nooki_when_flag_on(monkeypatch):
    """开关 true 时 install_central_routes 挂载 Nooki 用户侧路由。"""

    monkeypatch.setattr(http_module.settings, "nooki_product_enabled", True)
    app = FastAPI()
    http_module.install_central_routes(app)

    nooki_paths = _routes_containing(app, "/products/nooki")
    assert nooki_paths, "Nooki 路由应在开关开启时挂载"
    # 至少包含登录绑定端点（auth.router 提供）
    assert any("/auth/bind" in p for p in nooki_paths)


def test_zhaoxi_routes_mounted_regardless_of_nooki_flag(monkeypatch):
    """无论 Nooki 开关真假，zhaoxi 路由都正常挂载。"""

    for flag in (False, True):
        monkeypatch.setattr(http_module.settings, "nooki_product_enabled", flag)
        app = FastAPI()
        http_module.install_central_routes(app)
        zhaoxi_paths = _routes_containing(app, "/products/zhaoxi")
        assert zhaoxi_paths, f"zhaoxi 路由不应受 nooki 开关影响 (flag={flag})"
