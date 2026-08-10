"""MP-02 subscription、wallet、ledger 与 cost 的产品隔离。"""
import concurrent.futures
import inspect

import pytest

import app.db as db
from app.bootstrap.product_registry import build_test_product_registry
from app.db._backend import IntegrityError
from app.db._core import NEW_USER_GRANT_SHELL_MICROS
from app.db.billing import _shell_micros_for_tokens


_PRODUCT_SCOPED_API_PARAMETERS = {
    "get_or_create_personal_referral_code_for_user": "app_id",
    "validate_referral_code": "expected_app_id",
    "preview_referral_code": "expected_app_id",
    "register_platform_user_with_referral": "app_id",
    "upsert_subscription_for_user": "app_id",
    "get_latest_subscription_for_user": "app_id",
    "retry_qualified_referral_rewards_for_user": "app_id",
    "release_due_referral_rewards_for_user": "app_id",
    "release_due_referral_rewards": "app_id",
    "list_referral_relationships": "app_id",
    "create_ai4all_account_for_user": "app_id",
    "get_or_create_default_ai4all_account_for_user": "app_id",
}


def test_product_scoped_billing_apis_require_explicit_app_id(fresh_db):
    for function_name, parameter_name in _PRODUCT_SCOPED_API_PARAMETERS.items():
        parameter = inspect.signature(getattr(db, function_name)).parameters[
            parameter_name
        ]
        assert parameter.default is inspect.Parameter.empty

    with db.connect() as conn:
        accounts_before = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()[
            "n"
        ]
    with pytest.raises(TypeError, match="app_id"):
        db.create_ai4all_account_for_user(
            platform_user_id="missing-product-scope",
            display_name="不应创建的朝夕账号",
        )
    with db.connect() as conn:
        accounts_after = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()[
            "n"
        ]
    assert accounts_after == accounts_before


def _two_product_accounts(phone: str):
    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone=phone)
    zhaoxi = db.create_ai4all_account_for_user(
        app_id="zhaoxi",
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
    return registry, user["id"], zhaoxi["id"], test_product["id"]


def _balance(account_id: str, *, registry=None) -> int:
    kwargs = {"registry": registry} if registry is not None else {}
    summary = db.get_wallet_summary(
        account_id=account_id, create_if_missing=False, **kwargs
    )
    return int(summary["wallet"]["balance_shell_micros"])


def test_wallet_grant_ledger_and_cost_are_isolated_by_product(fresh_db):
    registry, user_id, zhaoxi_id, test_id = _two_product_accounts("13800037401")

    zhaoxi = db.get_wallet_summary(account_id=zhaoxi_id, create_if_missing=False)
    test_product = db.get_wallet_summary(
        account_id=test_id, create_if_missing=False, registry=registry
    )
    assert zhaoxi["wallet"]["app_id"] == "zhaoxi"
    assert test_product["wallet"]["app_id"] == "test_product"
    assert zhaoxi["wallet"]["id"] != test_product["wallet"]["id"]
    assert zhaoxi["wallet"]["balance_shell_micros"] == NEW_USER_GRANT_SHELL_MICROS
    assert test_product["wallet"]["balance_shell_micros"] == NEW_USER_GRANT_SHELL_MICROS

    # 每产品重复调用都只赠一次，且 test_product 使用产品化幂等键。
    db.grant_new_user_shells(account_id=zhaoxi_id, platform_user_id=user_id)
    db.grant_new_user_shells(
        account_id=test_id, platform_user_id=user_id, registry=registry
    )
    zhaoxi_before = _balance(zhaoxi_id)
    test_before = _balance(test_id, registry=registry)

    result = db.record_chat_usage_charge(
        account_id=test_id,
        model="deepseek-chat",
        messages=[{"role": "user", "content": "hello"}],
        reply="hi",
        source_type="chat",
        source_id="test-product-message",
        idempotency_key="test-product-charge",
        input_tokens=1000,
        output_tokens=200,
        registry=registry,
    )
    debit = _shell_micros_for_tokens(billable_tokens=1200)
    assert result["cost_event"]["app_id"] == "test_product"
    assert result["ledger"]["app_id"] == "test_product"
    assert _balance(test_id, registry=registry) == test_before - debit
    assert _balance(zhaoxi_id) == zhaoxi_before

    zhaoxi_ledger = db.list_wallet_ledger(account_id=zhaoxi_id)
    test_ledger = db.list_wallet_ledger(account_id=test_id, registry=registry)
    assert {row["app_id"] for row in zhaoxi_ledger} == {"zhaoxi"}
    assert {row["app_id"] for row in test_ledger} == {"test_product"}
    assert all(row["id"] not in {item["id"] for item in zhaoxi_ledger} for row in test_ledger)

    with db.connect() as conn:
        keys = {
            row["app_id"]: row["idempotency_key"]
            for row in conn.execute(
                """
                SELECT app_id, idempotency_key FROM entitlement_ledger
                WHERE platform_user_id=? AND source_type='new_user_grant'
                """,
                (user_id,),
            ).fetchall()
        }
    assert keys == {
        "zhaoxi": f"new-user-grant-{user_id}",
        "test_product": f"new-user-grant-test_product-{user_id}",
    }


def test_billing_idempotency_key_is_scoped_by_product(fresh_db):
    registry, _user_id, zhaoxi_id, test_id = _two_product_accounts("13800037408")
    shared_key = "shared-raw-charge-key"

    zhaoxi = db.record_chat_usage_charge(
        account_id=zhaoxi_id,
        model="deepseek-chat",
        messages=[{"role": "user", "content": "z"}],
        reply="ok",
        source_type="chat",
        source_id="zhaoxi-shared-key",
        idempotency_key=shared_key,
        input_tokens=1000,
        output_tokens=200,
    )
    test_product = db.record_chat_usage_charge(
        account_id=test_id,
        model="deepseek-chat",
        messages=[{"role": "user", "content": "t"}],
        reply="ok",
        source_type="chat",
        source_id="test-product-shared-key",
        idempotency_key=shared_key,
        input_tokens=1000,
        output_tokens=200,
        registry=registry,
    )

    assert zhaoxi["cost_event"]["id"] != test_product["cost_event"]["id"]
    assert zhaoxi["ledger"]["id"] != test_product["ledger"]["id"]
    with db.connect() as conn:
        cost_rows = conn.execute(
            """
            SELECT app_id FROM cost_events
            WHERE idempotency_key=? ORDER BY app_id
            """,
            (shared_key,),
        ).fetchall()
        ledger_rows = conn.execute(
            """
            SELECT app_id FROM entitlement_ledger
            WHERE idempotency_key=? ORDER BY app_id
            """,
            (f"usage-charge-{shared_key}",),
        ).fetchall()
    assert [row["app_id"] for row in cost_rows] == ["test_product", "zhaoxi"]
    assert [row["app_id"] for row in ledger_rows] == ["test_product", "zhaoxi"]


def test_billing_rejects_owner_or_membership_scope_mismatch(fresh_db):
    registry, user_id, zhaoxi_id, test_id = _two_product_accounts("13800037402")
    other = db.create_or_get_platform_user_by_phone(phone="13800037403")

    with pytest.raises(ValueError, match="billing scope mismatch"):
        db.grant_shells(
            account_id=test_id,
            platform_user_id=other["id"],
            amount_shell_micros=1_000_000,
            source_type="bad_scope",
            source_id=None,
            idempotency_key="bad-scope-grant",
            registry=registry,
        )

    db.update_product_membership_status(
        platform_user_id=user_id,
        app_id="test_product",
        status="disabled",
        registry=registry,
    )
    with pytest.raises(ValueError, match="active product membership required"):
        db.get_wallet_summary(
            account_id=test_id, create_if_missing=False, registry=registry
        )
    # 关闭 test_product membership 不影响 zhaoxi 资产读取。
    assert db.get_wallet_summary(
        account_id=zhaoxi_id, create_if_missing=False
    )["wallet"]["app_id"] == "zhaoxi"


def test_subscription_keeps_status_history_per_product(fresh_db):
    registry, user_id, _zhaoxi_id, _test_id = _two_product_accounts("13800037404")

    cancelled = db.upsert_subscription_for_user(
        app_id="zhaoxi",
        platform_user_id=user_id, plan="free", status="cancelled"
    )
    active = db.upsert_subscription_for_user(
        app_id="zhaoxi",
        platform_user_id=user_id, plan="pro", status="active"
    )
    test_active = db.upsert_subscription_for_user(
        platform_user_id=user_id,
        plan="test-pro",
        status="active",
        app_id="test_product",
        registry=registry,
    )

    assert cancelled["status"] == "cancelled"
    assert active["id"] != cancelled["id"]
    assert db.get_latest_subscription_for_user(
        platform_user_id=user_id,
        app_id="zhaoxi",
    )["id"] == active["id"]
    assert db.get_latest_subscription_for_user(
        platform_user_id=user_id,
        app_id="test_product",
        registry=registry,
    )["id"] == test_active["id"]
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT app_id, status, COUNT(*) AS n
            FROM subscriptions WHERE platform_user_id=?
            GROUP BY app_id, status
            """,
            (user_id,),
        ).fetchall()
    counts = {(row["app_id"], row["status"]): int(row["n"]) for row in rows}
    assert counts[("zhaoxi", "cancelled")] == 1
    assert counts[("zhaoxi", "active")] == 1
    assert counts[("test_product", "active")] == 1


def test_m0039_supersedes_only_older_active_subscription(fresh_db):
    from app.db._core import _migration_0039_billing_app_id_expand

    user = db.create_or_get_platform_user_by_phone(phone="13800037405")
    with db.connect() as conn:
        conn.execute("DROP INDEX IF EXISTS ux_subscriptions_user_app_active")
        conn.execute(
            """
            INSERT INTO subscriptions(
                id, platform_user_id, app_id, plan, status, updated_at
            ) VALUES ('sub-old', ?, 'zhaoxi', 'old', 'active', '2026-01-01 00:00:00')
            """,
            (user["id"],),
        )
        conn.execute(
            """
            INSERT INTO subscriptions(
                id, platform_user_id, app_id, plan, status, updated_at
            ) VALUES ('sub-new', ?, 'zhaoxi', 'new', 'active', '2026-01-02 00:00:00')
            """,
            (user["id"],),
        )
        conn.execute(
            """
            INSERT INTO subscriptions(
                id, platform_user_id, app_id, plan, status, updated_at
            ) VALUES ('sub-expired', ?, 'zhaoxi', 'old', 'expired', '2025-12-01 00:00:00')
            """,
            (user["id"],),
        )
        _migration_0039_billing_app_id_expand(conn)
        _migration_0039_billing_app_id_expand(conn)
        states = {
            row["id"]: row["status"]
            for row in conn.execute(
                "SELECT id, status FROM subscriptions WHERE platform_user_id=?",
                (user["id"],),
            ).fetchall()
        }
    assert states == {
        "sub-old": "superseded",
        "sub-new": "active",
        "sub-expired": "expired",
    }


def test_pg_concurrent_cross_product_charges_and_subscription_upserts(fresh_db):
    registry, user_id, zhaoxi_id, test_id = _two_product_accounts("13800037406")
    zhaoxi_before = _balance(zhaoxi_id)
    test_before = _balance(test_id, registry=registry)
    per_debit = _shell_micros_for_tokens(billable_tokens=1200)

    def charge(index: int):
        account_id = zhaoxi_id if index % 2 == 0 else test_id
        kwargs = {} if index % 2 == 0 else {"registry": registry}
        return db.record_chat_usage_charge(
            account_id=account_id,
            model="deepseek-chat",
            messages=[{"role": "user", "content": "x"}],
            reply="y",
            source_type="chat",
            source_id=f"cross-{index}",
            idempotency_key=f"cross-product-charge-{index}",
            input_tokens=1000,
            output_tokens=200,
            **kwargs,
        )

    def upsert_subscription(index: int):
        if index % 2 == 0:
            return db.upsert_subscription_for_user(
                app_id="zhaoxi",
                platform_user_id=user_id, plan="pro", status="active"
            )
        return db.upsert_subscription_for_user(
            platform_user_id=user_id,
            plan="test-pro",
            status="active",
            app_id="test_product",
            registry=registry,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        charges = list(executor.map(charge, range(12)))
        subscriptions = list(executor.map(upsert_subscription, range(12)))

    assert all(item is not None for item in charges)
    assert _balance(zhaoxi_id) == zhaoxi_before - per_debit * 6
    assert _balance(test_id, registry=registry) == test_before - per_debit * 6
    assert {item["app_id"] for item in subscriptions} == {"zhaoxi", "test_product"}
    with db.connect() as conn:
        active = conn.execute(
            """
            SELECT app_id, COUNT(*) AS n FROM subscriptions
            WHERE platform_user_id=? AND status='active'
            GROUP BY app_id
            """,
            (user_id,),
        ).fetchall()
    assert {row["app_id"]: int(row["n"]) for row in active} == {
        "zhaoxi": 1,
        "test_product": 1,
    }


def test_active_wallet_unique_constraint_is_per_product(fresh_db):
    _registry, user_id, _zhaoxi_id, _test_id = _two_product_accounts("13800037407")
    with db.connect() as conn:
        assert conn.execute(
            """
            SELECT COUNT(*) AS n FROM entitlement_wallets
            WHERE platform_user_id=? AND status='active'
            """,
            (user_id,),
        ).fetchone()["n"] == 2
    with pytest.raises(IntegrityError):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO accounts(id, app_id, display_name) "
                "VALUES ('second-zhaoxi-wallet-source', 'zhaoxi', '重复钱包来源')"
            )
            conn.execute(
                """
                INSERT INTO entitlement_wallets(
                    id, account_id, platform_user_id, app_id, status
                ) VALUES ('duplicate-zhaoxi-wallet', ?, ?, 'zhaoxi', 'active')
                """,
                ("second-zhaoxi-wallet-source", user_id),
            )
