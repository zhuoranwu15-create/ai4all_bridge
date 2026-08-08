"""MP-01 product registry 与 membership 原语。"""
import concurrent.futures
from unittest.mock import patch

import pytest

import app.db as db
from app.bootstrap.product_registry import (
    PLUM_APP_ID,
    MINGCHAN_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
    ZHAOXI_APP_ID,
    ProductRegistration,
    ProductRegistry,
    build_test_product_registry,
)
from app.db._backend import is_postgres


def test_production_registry_enables_all_shipped_products():
    registrations = PRODUCTION_PRODUCT_REGISTRY.registrations()
    assert [
        (product.app_id, product.enabled, product.default_language)
        for product in registrations
    ] == [
        (MINGCHAN_APP_ID, True, "zh-CN"),
        (PLUM_APP_ID, True, "zh-CN"),
        (ZHAOXI_APP_ID, True, "zh-CN"),
    ]
    assert PRODUCTION_PRODUCT_REGISTRY.require_enabled(MINGCHAN_APP_ID).enabled is True
    assert PRODUCTION_PRODUCT_REGISTRY.require_enabled(PLUM_APP_ID).enabled is True
    with pytest.raises(ValueError, match="unregistered app_id"):
        PRODUCTION_PRODUCT_REGISTRY.require_enabled("test_product")


def test_product_registry_validates_default_language():
    registry = ProductRegistry(
        [ProductRegistration(app_id="english_product", default_language="en-US")]
    )
    assert registry.require_enabled("english_product").default_language == "en-US"

    with pytest.raises(ValueError, match="unsupported default_language"):
        ProductRegistry(
            [ProductRegistration(app_id="bad_language", default_language="fr-FR")]
        )


def test_product_registry_enforces_static_channel_allowlist():
    registry = ProductRegistry(
        [
            ProductRegistration(
                app_id="channel_product",
                allowed_channels=("native",),
            )
        ]
    )

    assert registry.require_allowed_channel("channel_product", "native").app_id == (
        "channel_product"
    )
    with pytest.raises(ValueError, match="channel not allowed"):
        registry.require_allowed_channel("channel_product", "openclaw-weixin")


def test_platform_user_upsert_ensures_zhaoxi_membership_idempotently(fresh_db):
    first = db.create_or_get_platform_user_by_phone(phone="13800037101")
    second = db.create_or_get_platform_user_by_phone(phone="13800037101")

    memberships = db.list_product_memberships(platform_user_id=first["id"])
    assert second["id"] == first["id"]
    assert [(item["app_id"], item["status"]) for item in memberships] == [
        ("zhaoxi", "active")
    ]


def test_registration_returns_product_newness_and_rolls_back_with_membership(fresh_db):
    first = db.register_platform_user_with_referral(phone="13800037105")
    second = db.register_platform_user_with_referral(phone="13800037105")
    assert first["is_new_user"] is True
    assert first["is_new_membership"] is True
    assert second["is_new_user"] is False
    assert second["is_new_membership"] is False

    with patch(
        "app.db.billing._ensure_product_membership_in_conn",
        side_effect=RuntimeError("membership write failed"),
    ), pytest.raises(RuntimeError, match="membership write failed"):
        db.register_platform_user_with_referral(phone="13800037106")
    assert db.get_platform_user_by_phone(phone="13800037106") is None


def test_membership_creation_requires_registry_and_disabled_is_not_reactivated(fresh_db):
    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone="13800037102")

    created = db.ensure_product_membership(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    )
    replay = db.ensure_product_membership(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    )
    assert created["is_new_membership"] is True
    assert replay["is_new_membership"] is False

    disabled = db.update_product_membership_status(
        platform_user_id=user["id"],
        app_id="test_product",
        status="disabled",
        registry=registry,
    )
    replay_disabled = db.ensure_product_membership(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    )
    assert disabled["status"] == "disabled"
    assert replay_disabled["membership"]["status"] == "disabled"
    with pytest.raises(ValueError, match="active product membership required"):
        db.require_active_product_membership(
            platform_user_id=user["id"],
            app_id="test_product",
            registry=registry,
        )
    with pytest.raises(ValueError, match="unregistered app_id"):
        db.ensure_product_membership(
            platform_user_id=user["id"], app_id="unknown_product"
        )
    with pytest.raises(ValueError, match="platform_user not found"):
        db.ensure_product_membership(
            platform_user_id="missing-user",
            app_id="test_product",
            registry=registry,
        )


def test_m0037_backfills_quota_overrides_without_changing_user_count(fresh_db):
    from app.db._core import _migration_0037_product_memberships

    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO platform_users(id, phone, daily_limit, rpm_limit)
            VALUES ('legacy-m0037-user', '13800037103', 23, 7)
            """
        )
        users_before = conn.execute("SELECT COUNT(*) AS n FROM platform_users").fetchone()["n"]
        _migration_0037_product_memberships(conn)
        _migration_0037_product_memberships(conn)
        users_after = conn.execute("SELECT COUNT(*) AS n FROM platform_users").fetchone()["n"]
        row = conn.execute(
            """
            SELECT status, daily_limit, rpm_limit
            FROM product_memberships
            WHERE platform_user_id='legacy-m0037-user' AND app_id='zhaoxi'
            """
        ).fetchone()

    assert users_after == users_before
    assert dict(row) == {"status": "active", "daily_limit": 23, "rpm_limit": 7}


def test_concurrent_membership_ensure_creates_exactly_once(fresh_db):
    if not is_postgres():
        pytest.skip("membership 并发唯一性以 PG 为准")
    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone="13800037104")

    def ensure_once(_index: int) -> bool:
        return bool(
            db.ensure_product_membership(
                platform_user_id=user["id"],
                app_id="test_product",
                registry=registry,
            )["is_new_membership"]
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        created = list(executor.map(ensure_once, range(12)))

    assert created.count(True) == 1
    with db.connect() as conn:
        count = conn.execute(
            """
            SELECT COUNT(*) AS n FROM product_memberships
            WHERE platform_user_id=? AND app_id='test_product'
            """,
            (user["id"],),
        ).fetchone()["n"]
    assert count == 1
