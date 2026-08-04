"""鸣蝉 Native 身份 HTTP 边界。"""
from __future__ import annotations

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.db as db
from app.bootstrap.product_registry import (
    MINGCHAN_APP_ID,
    ZHAOXI_APP_ID,
    build_test_product_registry,
)
from app.products.mingchan.manifest import install_public_routes


def _verified_token(phone: str) -> str:
    verification = db.create_phone_verification(
        phone=phone,
        code="999999",
        expires_minutes=10,
    )
    verified = db.set_verification_verified(
        verification["id"],
        token_expires_minutes=10,
    )
    return str(verified["verified_token"])


def _enabled_mingchan_client(config) -> tuple[TestClient, object]:
    registry = build_test_product_registry()
    app = FastAPI()
    install_public_routes(app, registry=registry, config=config)
    return TestClient(app), registry


def test_mingchan_session_route_uses_only_fixed_product_namespaces(fresh_db):
    client, registry = _enabled_mingchan_client(fresh_db)
    phone = "13800037911"

    response = client.post(
        "/api/v1/products/mingchan/auth/session",
        json={"phone": phone, "verified_token": _verified_token(phone)},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["is_new_user"] is True
    assert payload["platform_user"]["phone_masked"] == "138****7911"
    principal = db.resolve_session_principal(
        token=payload["access_token"],
        expected_app_id=MINGCHAN_APP_ID,
        registry=registry,
    )
    assert principal is not None
    assert principal.app_id == MINGCHAN_APP_ID
    assert client.post("/v1/auth/session", json={}).status_code == 404


def test_mingchan_me_rejects_zhaoxi_token_and_accepts_mingchan_token(fresh_db):
    client, registry = _enabled_mingchan_client(fresh_db)
    user = db.create_or_get_platform_user_by_phone(phone="13800037912")
    db.ensure_product_membership(
        platform_user_id=user["id"],
        app_id=MINGCHAN_APP_ID,
        registry=registry,
    )
    zhaoxi_session = db.create_platform_user_session(
        platform_user_id=user["id"],
        app_id=ZHAOXI_APP_ID,
        registry=registry,
    )
    mingchan_session = db.create_platform_user_session(
        platform_user_id=user["id"],
        app_id=MINGCHAN_APP_ID,
        registry=registry,
    )

    zhaoxi_response = client.get(
        "/api/v1/products/mingchan/me",
        headers={"Authorization": f"Bearer {zhaoxi_session['token']}"},
    )
    mingchan_response = client.get(
        "/v1/products/mingchan/me",
        headers={"Authorization": f"Bearer {mingchan_session['token']}"},
    )

    assert zhaoxi_response.status_code == 401
    assert mingchan_response.status_code == 200
    assert mingchan_response.json()["platform_user"]["id"] == user["id"]


def test_mingchan_logout_revokes_only_presented_mingchan_session(fresh_db):
    client, _registry = _enabled_mingchan_client(fresh_db)
    phone = "13800037913"
    login = client.post(
        "/api/v1/products/mingchan/auth/session",
        json={"phone": phone, "verified_token": _verified_token(phone)},
    ).json()
    headers = {"Authorization": f"Bearer {login['access_token']}"}

    logout = client.delete(
        "/api/v1/products/mingchan/auth/session/current",
        headers=headers,
    )

    assert logout.status_code == 200
    assert client.get("/api/v1/products/mingchan/me", headers=headers).status_code == 401


def test_disabled_production_mingchan_route_fails_before_consuming_otp(fresh_db):
    app = FastAPI()
    install_public_routes(app)
    client = TestClient(app)
    phone = "13800037914"
    token = _verified_token(phone)

    response = client.post(
        "/api/v1/products/mingchan/auth/session",
        json={"phone": phone, "verified_token": token},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "产品暂不可用"
    assert db.get_valid_verification_by_token(token, phone) is not None


def test_mingchan_openapi_exposes_only_canonical_identity_paths(fresh_db):
    client, _registry = _enabled_mingchan_client(fresh_db)

    paths = set(client.get("/openapi.json").json()["paths"])

    assert "/api/v1/products/mingchan/auth/otp/send" in paths
    assert "/api/v1/products/mingchan/auth/otp/verify" in paths
    assert "/api/v1/products/mingchan/auth/session" in paths
    assert "/api/v1/products/mingchan/auth/session/current" in paths
    assert "/api/v1/products/mingchan/me" in paths
    assert not any(path.startswith("/v1/products/mingchan") for path in paths)
    assert "/v1/auth/session" not in paths


def test_mingchan_otp_to_session_login_is_self_contained(fresh_db):
    client, registry = _enabled_mingchan_client(fresh_db)
    phone = "13800037915"

    with patch(
        "app.products.mingchan.api.auth.verify_captcha",
        return_value=True,
    ), patch("app.products.mingchan.api.auth.send_otp") as sms:
        sent = client.post(
            "/api/v1/products/mingchan/auth/otp/send",
            json={"phone": phone, "captcha_verify_param": "captcha-ok"},
        )
    assert sent.status_code == 200
    sms.assert_called_once()
    verification = db.get_latest_active_verification(phone)
    assert verification is not None

    verified = client.post(
        "/api/v1/products/mingchan/auth/otp/verify",
        json={"phone": phone, "code": verification["code"]},
    )
    assert verified.status_code == 200, verified.text

    login = client.post(
        "/api/v1/products/mingchan/auth/session",
        json={
            "phone": phone,
            "verified_token": verified.json()["verified_token"],
        },
    )
    assert login.status_code == 200, login.text
    principal = db.resolve_session_principal(
        token=login.json()["access_token"],
        expected_app_id=MINGCHAN_APP_ID,
        registry=registry,
    )
    assert principal is not None
    memberships = db.list_product_memberships(
        platform_user_id=principal.platform_user_id
    )
    assert [item["app_id"] for item in memberships] == [MINGCHAN_APP_ID]


def test_mingchan_otp_errors_preserve_shared_security_rules(fresh_db):
    client, _registry = _enabled_mingchan_client(fresh_db)

    with patch(
        "app.products.mingchan.api.auth.verify_captcha",
        return_value=False,
    ), patch("app.products.mingchan.api.auth.send_otp") as sms:
        rejected = client.post(
            "/api/v1/products/mingchan/auth/otp/send",
            json={
                "phone": "13800037916",
                "captcha_verify_param": "captcha-bad",
            },
        )

    assert rejected.status_code == 400
    assert rejected.json()["detail"] == "验证码校验未通过"
    sms.assert_not_called()


def test_disabled_mingchan_otp_route_stops_before_captcha_and_db(fresh_db):
    app = FastAPI()
    install_public_routes(app, config=fresh_db)
    client = TestClient(app)

    with patch(
        "app.products.mingchan.api.auth.verify_captcha",
        side_effect=AssertionError("disabled product must not call captcha"),
    ):
        response = client.post(
            "/api/v1/products/mingchan/auth/otp/send",
            json={
                "phone": "13800037917",
                "captcha_verify_param": "unused",
            },
        )

    assert response.status_code == 503
    assert db.count_verifications_last_hour("13800037917") == 0
