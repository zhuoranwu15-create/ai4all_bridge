"""鸣蝉产品 manifest 与固定 namespace 契约。"""

from app.bootstrap.product_registry import MINGCHAN_APP_ID
from app.products.mingchan import manifest


def test_mingchan_manifest_declares_fixed_namespace_and_route_installer():
    assert manifest.APP_ID == MINGCHAN_APP_ID
    assert manifest.CANONICAL_API_PREFIX == "/api/v1/products/mingchan"
    assert manifest.PROXY_STRIPPED_API_PREFIX == "/v1/products/mingchan"
    assert "install_product_lifecycle" in manifest.__all__
    assert "install_public_routes" in manifest.__all__
