"""MP-03 daily、reservation、RPM、override 与 wipe 的产品隔离。"""
import concurrent.futures

import pytest

import app.db as db
from app.bootstrap.product_registry import build_test_product_registry
from app.db._backend import is_postgres
from app.db._core import (
    _migration_0041_quota_app_id_expand,
    _migration_0042_quota_app_id_contract,
    _quota_contract_violation_counts,
)
from app.platform.quota.rate_limiter import RateLimiter, product_rpm_subject

_DATE = "2026-07-24"


def _two_product_accounts(phone: str):
    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone=phone)
    zhaoxi = db.create_ai4all_account_for_user(
        app_id="zhaoxi",
        platform_user_id=user["id"], display_name="朝夕入口"
    )["account"]["id"]
    db.ensure_product_membership(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    )
    test_product = db.create_ai4all_account_for_user(
        platform_user_id=user["id"],
        display_name="测试产品入口",
        app_id="test_product",
        registry=registry,
    )["account"]["id"]
    return registry, user["id"], zhaoxi, test_product


def test_daily_usage_and_reservations_are_isolated_by_product(fresh_db):
    _registry, user_id, zhaoxi_id, test_id = _two_product_accounts("13800037601")

    zhaoxi_token = db.reserve_daily_quota(
        account_id=zhaoxi_id, date=_DATE, limit=1
    )
    test_token = db.reserve_daily_quota(account_id=test_id, date=_DATE, limit=1)
    assert zhaoxi_token and test_token
    assert db.reserve_daily_quota(account_id=zhaoxi_id, date=_DATE, limit=1) is None
    assert db.reserve_daily_quota(account_id=test_id, date=_DATE, limit=1) is None

    db.confirm_daily_quota(reservation_id=zhaoxi_token)
    db.rollback_daily_quota(reservation_id=test_token)
    db.increment_daily_usage(account_id=test_id, date=_DATE)

    assert db.get_daily_usage(account_id=zhaoxi_id, date=_DATE) == 1
    assert db.get_daily_usage(account_id=test_id, date=_DATE) == 1
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT app_id, message_count FROM daily_usage
            WHERE platform_user_id=? AND date=? ORDER BY app_id
            """,
            (user_id, _DATE),
        ).fetchall()
    assert [(row["app_id"], int(row["message_count"])) for row in rows] == [
        ("test_product", 1),
        ("zhaoxi", 1),
    ]


def test_override_and_rpm_are_isolated_by_product(fresh_db):
    _registry, user_id, zhaoxi_id, test_id = _two_product_accounts("13800037602")

    db.update_account(account_id=zhaoxi_id, daily_limit=5, rpm_limit=2)
    db.update_account(account_id=test_id, daily_limit=9, rpm_limit=4)
    zhaoxi_limits = db.resolve_effective_quota_limits(
        account_id=zhaoxi_id, default_daily=50, default_rpm=10
    )
    test_limits = db.resolve_effective_quota_limits(
        account_id=test_id, default_daily=50, default_rpm=10
    )
    assert (zhaoxi_limits["daily_limit"], zhaoxi_limits["rpm_limit"]) == (5, 2)
    assert (test_limits["daily_limit"], test_limits["rpm_limit"]) == (9, 4)
    assert zhaoxi_limits["app_id"] == "zhaoxi"
    assert test_limits["app_id"] == "test_product"

    limiter = RateLimiter()
    now = 1_800_000_000.0
    assert limiter.check_product_rpm(
        platform_user_id=user_id, app_id="zhaoxi", limit=2, _now=now
    )
    assert limiter.check_product_rpm(
        platform_user_id=user_id, app_id="zhaoxi", limit=2, _now=now
    )
    assert not limiter.check_product_rpm(
        platform_user_id=user_id, app_id="zhaoxi", limit=2, _now=now
    )
    assert limiter.check_product_rpm(
        platform_user_id=user_id, app_id="test_product", limit=2, _now=now
    )

    with db.connect() as conn:
        memberships = {
            row["app_id"]: (row["daily_limit"], row["rpm_limit"])
            for row in conn.execute(
                """
                SELECT app_id, daily_limit, rpm_limit FROM product_memberships
                WHERE platform_user_id=?
                """,
                (user_id,),
            ).fetchall()
        }
    assert memberships == {"test_product": (9, 4), "zhaoxi": (5, 2)}


def test_wipe_removes_only_current_product_quota(fresh_db):
    _registry, user_id, zhaoxi_id, test_id = _two_product_accounts("13800037603")
    db.increment_daily_usage(account_id=zhaoxi_id, date=_DATE)
    db.increment_daily_usage(account_id=test_id, date=_DATE)
    zhaoxi_token = db.reserve_daily_quota(
        account_id=zhaoxi_id, date="2026-07-25", limit=5
    )
    test_token = db.reserve_daily_quota(
        account_id=test_id, date="2026-07-25", limit=5
    )

    stats = db.wipe_account_data(account_id=test_id)

    assert stats["daily_usage_deleted"] == 1
    assert stats["daily_quota_reservations_deleted"] == 1
    assert db.get_daily_usage(account_id=zhaoxi_id, date=_DATE) == 1
    with db.connect() as conn:
        daily_apps = {
            row["app_id"]
            for row in conn.execute(
                "SELECT app_id FROM daily_usage WHERE platform_user_id=?",
                (user_id,),
            ).fetchall()
        }
        reservation_ids = {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM daily_quota_reservations WHERE platform_user_id=?",
                (user_id,),
            ).fetchall()
        }
    assert daily_apps == {"zhaoxi"}
    assert zhaoxi_token in reservation_ids
    assert test_token not in reservation_ids


def test_expand_is_idempotent_and_contract_preserves_zhaoxi_rpm_window(fresh_db):
    _registry, user_id, zhaoxi_id, _test_id = _two_product_accounts("13800037604")
    db.increment_daily_usage(account_id=zhaoxi_id, date=_DATE)
    with db.connect() as conn:
        _migration_0041_quota_app_id_expand(conn)
        _migration_0041_quota_app_id_expand(conn)
        conn.execute(
            "INSERT INTO rpm_hits(account_id, hit_at) VALUES (?, ?)",
            (user_id, 1_800_000_000.0),
        )
        conn.execute(
            "INSERT INTO rpm_hits(account_id, hit_at) VALUES (?, ?)",
            (zhaoxi_id, 1_800_000_001.0),
        )
        _migration_0042_quota_app_id_contract(conn)
        old_hits = conn.execute(
            "SELECT COUNT(*) AS n FROM rpm_hits WHERE account_id=?", (user_id,)
        ).fetchone()["n"]
        new_hits = conn.execute(
            "SELECT COUNT(*) AS n FROM rpm_hits WHERE account_id=?",
            (product_rpm_subject(platform_user_id=user_id, app_id="zhaoxi"),),
        ).fetchone()["n"]
        old_account_hits = conn.execute(
            "SELECT COUNT(*) AS n FROM rpm_hits WHERE account_id=?", (zhaoxi_id,)
        ).fetchone()["n"]
    assert int(old_hits) == 0
    assert int(old_account_hits) == 0
    assert int(new_hits) == 2


def test_contract_rejects_quota_scope_drift(fresh_db):
    _registry, _user_id, zhaoxi_id, _test_id = _two_product_accounts("13800037605")
    db.increment_daily_usage(account_id=zhaoxi_id, date=_DATE)
    with db.connect() as conn:
        conn.execute(
            "UPDATE daily_usage SET app_id='test_product' WHERE account_id=?",
            (zhaoxi_id,),
        )
        counts = _quota_contract_violation_counts(conn)
        assert counts["daily_scope_drift"] == 1
        with pytest.raises(RuntimeError, match="m0042 quota reconcile failed"):
            _migration_0042_quota_app_id_contract(conn)


def test_pg_concurrent_reservation_is_product_independent(fresh_db):
    if not is_postgres():
        pytest.skip("跨产品 quota advisory lock 并发以 PostgreSQL 为准")
    _registry, _user_id, zhaoxi_id, test_id = _two_product_accounts("13800037606")

    def reserve(index: int):
        account_id = zhaoxi_id if index % 2 == 0 else test_id
        return account_id, db.reserve_daily_quota(
            account_id=account_id,
            date=_DATE,
            limit=2,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(reserve, range(12)))

    granted = {zhaoxi_id: 0, test_id: 0}
    for account_id, token in results:
        granted[account_id] += int(token is not None)
    assert granted == {zhaoxi_id: 2, test_id: 2}
