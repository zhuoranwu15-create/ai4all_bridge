"""鸣蝉 App 配置与真人资料 HTTP 边界。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.db as db
from app.bootstrap.product_registry import (
    MINGCHAN_APP_ID,
    ZHAOXI_APP_ID,
    build_test_product_registry,
)
from app.products.mingchan.application.identity import create_mingchan_login_session
from app.products.mingchan.manifest import install_public_routes


def _verified_token(phone: str) -> str:
    row = db.create_phone_verification(
        phone=phone,
        code="999999",
        expires_minutes=10,
    )
    return str(
        db.set_verification_verified(row["id"], token_expires_minutes=10)[
            "verified_token"
        ]
    )


def _client_login(config, phone: str) -> tuple[TestClient, dict, object]:
    registry = build_test_product_registry()
    app = FastAPI()
    install_public_routes(app, registry=registry, config=config)
    login = create_mingchan_login_session(
        phone=phone,
        verified_token=_verified_token(phone),
        registry=registry,
    )
    headers = {"Authorization": f"Bearer {login['session']['token']}"}
    return TestClient(app), headers, registry


def test_mingchan_app_config_uses_fixed_product_and_capability_surface(fresh_db):
    fresh_db.mingchan_p1_enabled = True
    fresh_db.mingchan_feed_enabled = True
    client, _headers, _registry = _client_login(fresh_db, "13800037950")

    response = client.get("/api/v1/products/mingchan/app/config")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    data = response.json()
    assert data["product"] == {
        "app_id": MINGCHAN_APP_ID,
        "default_language": "zh-CN",
    }
    assert data["features"]["resident_world"] is True
    assert data["features"]["world_feed"] is True
    assert "companion_world_p1_enabled" not in response.text
    assert client.get("/v1/app/config").status_code == 404


def test_mingchan_profile_options_and_update_require_mingchan_audience(fresh_db):
    client, headers, registry = _client_login(fresh_db, "13800037951")

    options = client.get(
        "/api/v1/products/mingchan/me/profile-options",
        headers=headers,
    )
    updated = client.patch(
        "/v1/products/mingchan/me/profile",
        headers=headers,
        json={"display_name": "鸣蝉用户", "avatar_key": "user_01"},
    )

    assert options.status_code == 200
    assert len(options.json()["avatars"]) == 8
    assert updated.status_code == 200
    assert updated.json()["platform_user"]["display_name"] == "鸣蝉用户"
    assert updated.json()["platform_user"]["avatar_key"] == "user_01"
    assert updated.json()["platform_user"]["avatar_ref"].endswith(
        "/companion_world/avatars/user/user_01.png"
    )

    principal = db.resolve_session_principal(
        token=headers["Authorization"].removeprefix("Bearer "),
        expected_app_id=MINGCHAN_APP_ID,
        registry=registry,
    )
    db.ensure_product_membership(
        platform_user_id=principal.platform_user_id,
        app_id=ZHAOXI_APP_ID,
        registry=registry,
    )
    zhaoxi = db.create_platform_user_session(
        platform_user_id=principal.platform_user_id,
        app_id=ZHAOXI_APP_ID,
        registry=registry,
    )
    denied = client.patch(
        "/api/v1/products/mingchan/me/profile",
        headers={"Authorization": f"Bearer {zhaoxi['token']}"},
        json={"display_name": "不应更新"},
    )
    assert denied.status_code == 401
    stored = db.get_platform_user(platform_user_id=principal.platform_user_id)
    assert stored["display_name"] == "鸣蝉用户"


def test_mingchan_profile_validation_is_fail_closed(fresh_db):
    client, headers, _registry = _client_login(fresh_db, "13800037952")

    empty = client.patch(
        "/api/v1/products/mingchan/me/profile",
        headers=headers,
        json={},
    )
    invalid_avatar = client.patch(
        "/api/v1/products/mingchan/me/profile",
        headers=headers,
        json={"avatar_key": "https://example.com/avatar.png"},
    )
    invalid_name = client.patch(
        "/api/v1/products/mingchan/me/profile",
        headers=headers,
        json={"display_name": "bad<script>"},
    )

    assert empty.status_code == 422
    assert empty.json()["detail"] == "profile_update_empty"
    assert invalid_avatar.status_code == 422
    assert invalid_avatar.json()["detail"] == "avatar_key_invalid"
    assert invalid_name.status_code == 422
    assert invalid_name.json()["detail"] == "nickname_charset_invalid"


def test_disabled_mingchan_app_config_fails_closed(fresh_db):
    app = FastAPI()
    registry = build_test_product_registry(mingchan_enabled=False)
    install_public_routes(app, registry=registry, config=fresh_db)

    response = TestClient(app).get("/api/v1/products/mingchan/app/config")

    assert response.status_code == 503
    assert response.json()["detail"] == "产品暂不可用"
