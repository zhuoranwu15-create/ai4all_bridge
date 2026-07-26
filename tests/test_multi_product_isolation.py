"""MP-05 Phase 1 双产品隔离矩阵与最终 contract 验收。"""
from __future__ import annotations

import pytest

import app.db as db
from app.bootstrap.product_registry import build_test_product_registry
from app.db._backend import is_postgres
from app.db._core import (
    _migration_0045_multi_product_phase1_contract,
    _phase1_contract_violation_counts,
    migrate_db_through,
)
from app.platform.quota.rate_limiter import RateLimiter


def _two_product_accounts(phone: str):
    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone=phone)
    zhaoxi = db.create_ai4all_account_for_user(
        platform_user_id=user["id"], display_name="朝夕入口"
    )["account"]
    db.ensure_product_membership(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    )
    test_product = db.create_ai4all_account_for_user(
        platform_user_id=user["id"],
        display_name="测试产品入口",
        app_id="test_product",
        registry=registry,
    )["account"]
    return registry, user, zhaoxi, test_product


def _runtime_session(account_id: str, suffix: str):
    return db.get_or_create_session(
        account_id=account_id,
        channel="native",
        sender_id=f"sender-{suffix}",
        sender_name=None,
        chat_id=f"chat-{suffix}",
        session_key=f"session-{suffix}",
    )["session"]


def _insert_user_message(account_id: str, session_id: int, content: str) -> int:
    message_id = db.insert_message(
        account_id=account_id,
        session_id=session_id,
        message_id=f"message-{account_id}-{content}",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content=content,
    )
    assert message_id is not None
    return message_id


def test_identity_token_message_and_onboarding_are_product_isolated(fresh_db):
    registry, user, zhaoxi, test_product = _two_product_accounts("13800037801")
    zhaoxi_token = db.create_platform_user_session(
        platform_user_id=user["id"], app_id="zhaoxi"
    )
    test_token = db.create_platform_user_session(
        platform_user_id=user["id"],
        app_id="test_product",
        registry=registry,
    )

    assert db.resolve_session_principal(
        token=zhaoxi_token["token"], expected_app_id="zhaoxi"
    ).app_id == "zhaoxi"
    assert db.resolve_session_principal(
        token=test_token["token"],
        expected_app_id="test_product",
        registry=registry,
    ).app_id == "test_product"
    assert db.resolve_session_principal(
        token=zhaoxi_token["token"],
        expected_app_id="test_product",
        registry=registry,
    ) is None
    assert db.resolve_session_principal(
        token=test_token["token"], expected_app_id="zhaoxi", registry=registry
    ) is None

    zhaoxi_resolved = db.get_active_bound_account_for_user_in_app(
        platform_user_id=user["id"], app_id="zhaoxi"
    )
    test_resolved = db.get_active_bound_account_for_user_in_app(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    )
    assert zhaoxi_resolved["account"]["id"] == zhaoxi["id"]
    assert test_resolved["account"]["id"] == test_product["id"]

    zhaoxi_session = _runtime_session(zhaoxi["id"], "zhaoxi")
    test_session = _runtime_session(test_product["id"], "test-product")
    _insert_user_message(zhaoxi["id"], int(zhaoxi_session["id"]), "仅朝夕可见")
    _insert_user_message(
        test_product["id"], int(test_session["id"]), "仅测试产品可见"
    )
    assert [
        row["content"]
        for row in db.list_recent_messages_for_account(
            account_id=zhaoxi["id"], limit=10
        )
    ] == ["仅朝夕可见"]
    assert [
        row["content"]
        for row in db.list_recent_messages_for_account(
            account_id=test_product["id"], limit=10
        )
    ] == ["仅测试产品可见"]

    db.set_account_onboarding_state(account_id=zhaoxi["id"], state="complete")
    db.set_account_onboarding_state(
        account_id=test_product["id"], state="step2_sent"
    )
    assert db.get_account_onboarding_state(account_id=zhaoxi["id"]) == "complete"
    assert (
        db.get_account_onboarding_state(account_id=test_product["id"])
        == "step2_sent"
    )


def test_quota_rpm_billing_subscription_and_referral_are_product_isolated(fresh_db):
    registry, user, zhaoxi, test_product = _two_product_accounts("13800037802")
    db.update_account(account_id=zhaoxi["id"], daily_limit=1, rpm_limit=1)
    db.update_account(account_id=test_product["id"], daily_limit=2, rpm_limit=2)

    zhaoxi_reservation = db.reserve_daily_quota(
        account_id=zhaoxi["id"], date="2026-07-24", limit=1
    )
    test_reservation = db.reserve_daily_quota(
        account_id=test_product["id"], date="2026-07-24", limit=1
    )
    assert zhaoxi_reservation and test_reservation
    db.confirm_daily_quota(reservation_id=zhaoxi_reservation)
    db.confirm_daily_quota(reservation_id=test_reservation)
    assert db.get_daily_usage(account_id=zhaoxi["id"], date="2026-07-24") == 1
    assert db.get_daily_usage(account_id=test_product["id"], date="2026-07-24") == 1

    limiter = RateLimiter()
    now = 1_800_000_000.0
    assert limiter.check_product_rpm(
        platform_user_id=user["id"], app_id="zhaoxi", limit=1, _now=now
    )
    assert not limiter.check_product_rpm(
        platform_user_id=user["id"], app_id="zhaoxi", limit=1, _now=now
    )
    assert limiter.check_product_rpm(
        platform_user_id=user["id"], app_id="test_product", limit=1, _now=now
    )

    zhaoxi_wallet = db.get_wallet_summary(
        account_id=zhaoxi["id"], create_if_missing=False
    )["wallet"]
    test_wallet = db.get_wallet_summary(
        account_id=test_product["id"], create_if_missing=False, registry=registry
    )["wallet"]
    assert zhaoxi_wallet["app_id"] == "zhaoxi"
    assert test_wallet["app_id"] == "test_product"
    assert zhaoxi_wallet["id"] != test_wallet["id"]

    zhaoxi_subscription = db.get_latest_subscription_for_user(
        platform_user_id=user["id"], app_id="zhaoxi"
    )
    test_subscription = db.get_latest_subscription_for_user(
        platform_user_id=user["id"],
        app_id="test_product",
        registry=registry,
    )
    assert zhaoxi_subscription["app_id"] == "zhaoxi"
    assert test_subscription["app_id"] == "test_product"

    zhaoxi_code = db.get_or_create_personal_referral_code_for_user(
        platform_user_id=user["id"], app_id="zhaoxi"
    )
    test_code = db.get_or_create_personal_referral_code_for_user(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    )
    assert not db.validate_referral_code(
        code=zhaoxi_code["code"],
        expected_app_id="test_product",
        registry=registry,
    )["valid"]
    invitee_phone = "13800037803"
    zhaoxi_registration = db.register_platform_user_with_referral(
        phone=invitee_phone, invite_code=zhaoxi_code["code"], app_id="zhaoxi"
    )
    test_registration = db.register_platform_user_with_referral(
        phone=invitee_phone,
        invite_code=test_code["code"],
        app_id="test_product",
        registry=registry,
    )
    assert zhaoxi_registration["referral_relationship"]["app_id"] == "zhaoxi"
    assert test_registration["is_new_membership"] is True
    assert (
        test_registration["referral_relationship"]["app_id"] == "test_product"
    )


def test_wipe_removes_only_target_product_runtime_and_assets(fresh_db):
    registry, user, zhaoxi, test_product = _two_product_accounts("13800037804")
    zhaoxi_session = _runtime_session(zhaoxi["id"], "wipe-zhaoxi")
    test_session = _runtime_session(test_product["id"], "wipe-test")
    _insert_user_message(zhaoxi["id"], int(zhaoxi_session["id"]), "保留")
    _insert_user_message(test_product["id"], int(test_session["id"]), "删除")
    db.increment_daily_usage(account_id=zhaoxi["id"], date="2026-07-25")
    db.increment_daily_usage(account_id=test_product["id"], date="2026-07-25")

    stats = db.wipe_account_data(account_id=test_product["id"])

    assert stats["messages_deleted"] == 1
    assert stats["entitlement_wallets_deleted"] == 1
    assert db.list_recent_messages_for_account(account_id=test_product["id"], limit=10) == []
    assert [
        row["content"]
        for row in db.list_recent_messages_for_account(
            account_id=zhaoxi["id"], limit=10
        )
    ] == ["保留"]
    assert db.get_daily_usage(account_id=zhaoxi["id"], date="2026-07-25") == 1
    assert db.get_wallet_summary(
        account_id=zhaoxi["id"], create_if_missing=False
    )["wallet"]["app_id"] == "zhaoxi"
    assert db.get_active_bound_account_for_user_in_app(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    ) is None


def test_m0045_final_contract_is_clean_idempotent_and_has_final_indexes(fresh_db):
    with db.connect() as conn:
        assert all(total == 0 for total in _phase1_contract_violation_counts(conn).values())
        _migration_0045_multi_product_phase1_contract(conn)
        _migration_0045_multi_product_phase1_contract(conn)
        if is_postgres():
            rows = conn.execute(
                "SELECT indexname AS name FROM pg_indexes WHERE schemaname=current_schema()"
            ).fetchall()
            nullable_app_columns = conn.execute(
                """
                SELECT table_name
                FROM information_schema.columns
                WHERE table_schema=current_schema() AND column_name='app_id'
                  AND is_nullable<>'NO'
                """
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
            nullable_app_columns = []
            for table in (
                "accounts",
                "account_owner_bindings",
                "platform_user_sessions",
                "product_memberships",
                "subscriptions",
                "entitlement_wallets",
                "entitlement_ledger",
                "cost_events",
                "daily_usage",
                "daily_quota_reservations",
                "referral_codes",
                "referral_relationships",
                "meaningful_message_reviews",
            ):
                app_column = next(
                    row
                    for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                    if row["name"] == "app_id"
                )
                if int(app_column["notnull"]) != 1:
                    nullable_app_columns.append({"table_name": table})
        index_names = {str(row["name"]) for row in rows}
        version = conn.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()["version"]

    assert int(version) == 47
    assert nullable_app_columns == []
    assert {
        "ix_accounts_app_status",
        "ux_owner_binding_active_user_app",
        "ux_entitlement_wallets_user_app_active",
        "ux_subscriptions_user_app_active",
        "ux_daily_usage_user_app_date",
        "ux_referral_relationships_invitee_app",
        "ux_entitlement_ledger_app_idempotency",
        "ux_cost_events_app_idempotency",
    } <= index_names


def test_m0045_blocks_cross_account_message_drift(fresh_db):
    _registry, _user, zhaoxi, test_product = _two_product_accounts("13800037805")
    test_session = _runtime_session(test_product["id"], "drift")
    _insert_user_message(zhaoxi["id"], int(test_session["id"]), "错误串号")

    with db.connect() as conn:
        counts = _phase1_contract_violation_counts(conn)
        assert counts["message_account_drift"] == 1
        with pytest.raises(RuntimeError, match="m0045 phase1 reconcile failed"):
            _migration_0045_multi_product_phase1_contract(conn)


def test_checkpoint_migration_runner_requires_expected_version(fresh_db):
    with db.connect() as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version>=45")

    assert migrate_db_through(
        target_version=45, expected_current_version=44
    ) == {"before": 44, "after": 45}
    assert migrate_db_through(
        target_version=45, expected_current_version=44
    ) == {"before": 45, "after": 45}
    assert migrate_db_through(
        target_version=46, expected_current_version=45
    ) == {"before": 45, "after": 46}
    assert migrate_db_through(
        target_version=46, expected_current_version=45
    ) == {"before": 46, "after": 46}
    with pytest.raises(RuntimeError, match="migration start version mismatch"):
        migrate_db_through(target_version=44, expected_current_version=43)
