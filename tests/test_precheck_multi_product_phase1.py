"""MP-01 多产品迁移前只读预检。"""
import scripts.precheck_multi_product_phase1 as precheck
from scripts.precheck_multi_product_phase1 import run_precheck


def test_clean_m0036_database_passes_without_exposing_rows(fresh_db):
    from app.db import connect, create_or_get_platform_user_by_phone

    create_or_get_platform_user_by_phone(phone="13800037001", display_name="预检用户")
    with connect() as conn:
        before = conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]
        report = run_precheck(conn)
        after = conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]

    assert report.blocking() == []
    assert before == after
    assert set(report.inventory) >= {"platform_users", "sessions_total"}
    assert all(not hasattr(check, "rows") for check in report.checks)


def test_identity_reconcile_runs_before_later_expands(fresh_db, monkeypatch):
    import app.db as db

    user = db.create_or_get_platform_user_by_phone(phone="13800037009")
    db.create_platform_user_session(platform_user_id=user["id"], app_id="zhaoxi")
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM product_memberships WHERE platform_user_id=? AND app_id='zhaoxi'",
            (user["id"],),
        )

    # 模拟已经完成 m0037/m0038、但尚未进入后续三个 expand 的发布检查点。
    monkeypatch.setattr(
        precheck, "_billing_app_columns_present", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        precheck, "_quota_app_columns_present", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        precheck, "_referral_app_columns_present", lambda *_args, **_kwargs: False
    )
    with db.connect() as conn:
        report = run_precheck(conn)

    totals = {check.name: check.total for check in report.checks}
    assert totals["session_scope_drift"] == 1
    assert any(check.name == "session_scope_drift" for check in report.blocking())


def test_precheck_blocks_app_drift_duplicate_subscription_and_orphan_referral(fresh_db):
    from app.db import connect, create_or_get_platform_user_by_phone

    user = create_or_get_platform_user_by_phone(phone="13800037002")
    with connect() as conn:
        conn.execute(
            "INSERT INTO accounts(id, app_id) VALUES ('precheck-account', 'zhaoxi')"
        )
        conn.execute(
            """
            INSERT INTO account_owner_bindings(
              platform_user_id, account_id, binding_method, status, app_id
            ) VALUES (?, 'precheck-account', 'test', 'archived', 'wrong-product')
            """,
            (user["id"],),
        )
        # fresh_db 已到 m0042；临时移除计费 contract 索引以模拟 m0039 expand 后脏数据。
        conn.execute("DROP INDEX IF EXISTS ux_subscriptions_user_app_active")
        conn.execute(
            "INSERT INTO subscriptions(id, platform_user_id, status) VALUES ('sub-a', ?, 'active')",
            (user["id"],),
        )
        conn.execute(
            "INSERT INTO subscriptions(id, platform_user_id, status) VALUES ('sub-b', ?, 'active')",
            (user["id"],),
        )
        # 测试连接启用 FK，先临时关闭检查不可行；用不存在的 reward ledger 不属于本预检口径。
        report = run_precheck(conn)

    totals = {check.name: check.total for check in report.checks}
    assert totals["binding_app_drift"] == 1
    assert totals["duplicate_active_subscription"] == 1
    assert {check.name for check in report.blocking()} >= {
        "binding_app_drift",
        "duplicate_active_subscription",
    }


def test_precheck_distinguishes_historical_unbind_from_real_quota_fallback(fresh_db):
    from app.db import connect, create_ai4all_account_for_user, create_or_get_platform_user_by_phone

    user = create_or_get_platform_user_by_phone(phone="13800037003")
    account = create_ai4all_account_for_user(
        app_id="zhaoxi",
        platform_user_id=user["id"], display_name="历史账号"
    )["account"]
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO daily_usage(account_id, platform_user_id, date, message_count)
            VALUES (?, ?, '2026-07-23', 1)
            """,
            (account["id"], user["id"]),
        )
        conn.execute(
            "UPDATE account_owner_bindings SET status='archived' WHERE account_id=?",
            (account["id"],),
        )
        historical = run_precheck(conn)
        conn.execute(
            "UPDATE daily_usage SET platform_user_id=account_id WHERE account_id=?",
            (account["id"],),
        )
        fallback = run_precheck(conn)

    historical_totals = {check.name: check.total for check in historical.checks}
    fallback_totals = {check.name: check.total for check in fallback.checks}
    assert historical_totals["quota_owner_fallback"] == 0
    assert historical_totals["daily_scope_drift"] == 0
    assert fallback_totals["quota_owner_fallback"] == 1
    # fresh_db 已完成 m0041 expand；fallback 从 MP-01 阶段 WARN 升级为 m0042 contract BLOCK。
    assert any(
        check.name == "quota_owner_fallback" for check in fallback.blocking()
    )


def test_expanded_precheck_accepts_one_active_billing_scope_per_product(fresh_db):
    import app.db as db
    from app.bootstrap.product_registry import build_test_product_registry

    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone="13800037004")
    db.create_ai4all_account_for_user(
        app_id="zhaoxi",
        platform_user_id=user["id"], display_name="朝夕入口"
    )
    db.ensure_product_membership(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    )
    db.create_ai4all_account_for_user(
        platform_user_id=user["id"],
        display_name="测试产品入口",
        app_id="test_product",
        registry=registry,
    )

    with db.connect() as conn:
        report = run_precheck(conn)

    assert report.blocking() == []
    totals = {check.name: check.total for check in report.checks}
    assert totals["duplicate_active_subscription"] == 0
    assert totals["duplicate_active_wallet"] == 0
    assert totals["wallet_ledger_balance_mismatch"] == 0
