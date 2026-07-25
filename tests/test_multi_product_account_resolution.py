"""MP-01 入口账号按 (platform_user_id, app_id) 隔离。"""
import concurrent.futures

import pytest

import app.db as db
from app.bootstrap.product_registry import build_test_product_registry
from app.db._backend import is_postgres


def test_same_user_resolves_distinct_entry_account_per_product(fresh_db):
    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone="13800037301")
    zhaoxi = db.create_ai4all_account_for_user(
        platform_user_id=user["id"], display_name="朝夕入口"
    )
    db.ensure_product_membership(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    )
    test_product = db.create_ai4all_account_for_user(
        platform_user_id=user["id"],
        display_name="测试产品入口",
        app_id="test_product",
        registry=registry,
    )

    resolved_zhaoxi = db.get_active_bound_account_for_user_in_app(
        platform_user_id=user["id"], app_id="zhaoxi"
    )
    resolved_test = db.get_active_bound_account_for_user_in_app(
        platform_user_id=user["id"],
        app_id="test_product",
        registry=registry,
    )
    assert resolved_zhaoxi["account"]["id"] == zhaoxi["account"]["id"]
    assert resolved_test["account"]["id"] == test_product["account"]["id"]
    assert resolved_zhaoxi["account"]["id"] != resolved_test["account"]["id"]
    assert db.get_active_bound_account_for_user_in_app(
        platform_user_id=user["id"], app_id="zhaoxi"
    )["account"]["id"] == zhaoxi["account"]["id"]


def test_missing_or_disabled_membership_cannot_create_or_resolve_account(fresh_db):
    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone="13800037302")

    with pytest.raises(ValueError, match="active product membership required"):
        db.create_ai4all_account_for_user(
            platform_user_id=user["id"],
            display_name="不可创建",
            app_id="test_product",
            registry=registry,
        )
    db.ensure_product_membership(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    )
    db.update_product_membership_status(
        platform_user_id=user["id"],
        app_id="test_product",
        status="disabled",
        registry=registry,
    )
    with pytest.raises(ValueError, match="active product membership required"):
        db.create_ai4all_account_for_user(
            platform_user_id=user["id"],
            display_name="仍不可创建",
            app_id="test_product",
            registry=registry,
        )
    assert (
        db.get_active_bound_account_for_user_in_app(
            platform_user_id=user["id"],
            app_id="test_product",
            registry=registry,
        )
        is None
    )


def test_disabled_membership_cannot_create_resident_runtime_account(fresh_db):
    user = db.create_or_get_platform_user_by_phone(phone="13800037304")
    universe = db.get_or_create_home_universe(platform_user_id=user["id"])
    template = db.create_character_template(
        source_type="user_created",
        name="不可创建居民",
        avatar_ref=None,
        summary=None,
        tags_json="[]",
        persona_seed_json="{}",
        persona_version="v1",
    )
    db.update_product_membership_status(
        platform_user_id=user["id"], app_id="zhaoxi", status="disabled"
    )

    with pytest.raises(ValueError, match="active product membership required"):
        db.create_resident_runtime_account(
            universe_id=universe["id"],
            character_template_id=template["id"],
            display_name="不可创建居民",
        )


def test_concurrent_entry_account_creation_keeps_one_per_user_app(fresh_db):
    if not is_postgres():
        pytest.skip("入口账号并发唯一性以 PG 为准")
    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone="13800037303")
    db.ensure_product_membership(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    )

    def create_once(index: int) -> str:
        try:
            db.create_ai4all_account_for_user(
                platform_user_id=user["id"],
                display_name=f"入口{index}",
                app_id="test_product",
                registry=registry,
            )
            return "created"
        except ValueError as err:
            assert "already has an active account" in str(err)
            return "duplicate"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create_once, range(2)))

    assert sorted(results) == ["created", "duplicate"]
    with db.connect() as conn:
        count = conn.execute(
            """
            SELECT COUNT(*) AS n FROM account_owner_bindings
            WHERE platform_user_id=? AND app_id='test_product' AND status='active'
            """,
            (user["id"],),
        ).fetchone()["n"]
    assert count == 1
