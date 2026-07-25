"""D-09：真人级 quota override 来源与遗留冲突兼容。"""
import app.db as db
from app.db._core import _migration_0031_platform_user_quota_overrides
from tests.factories import make_resident_account


def _user_with_resident(phone: str):
    user = db.create_or_get_platform_user_by_phone(phone=phone, display_name="配额用户")
    primary = db.create_ai4all_account_for_user(
        platform_user_id=user["id"], display_name="主账号"
    )["account"]["id"]
    resident = make_resident_account(user["id"], "居民")
    return user["id"], primary, resident


def test_admin_override_is_canonical_for_all_residents(fresh_db):
    user_id, primary, resident = _user_with_resident("13800024001")

    db.update_account(account_id=primary, daily_limit=7, rpm_limit=2)

    for account_id in (primary, resident):
        limits = db.resolve_effective_quota_limits(
            account_id=account_id, default_daily=50, default_rpm=10
        )
        assert limits["daily_limit"] == 7
        assert limits["rpm_limit"] == 2
        assert limits["platform_user_id"] == user_id
    with db.connect() as conn:
        membership = conn.execute(
            "SELECT daily_limit, rpm_limit FROM product_memberships "
            "WHERE platform_user_id=? AND app_id='zhaoxi'",
            (user_id,),
        ).fetchone()
        copies = conn.execute(
            "SELECT daily_limit, rpm_limit FROM accounts WHERE id IN (?, ?)",
            (primary, resident),
        ).fetchall()
    assert (membership["daily_limit"], membership["rpm_limit"]) == (7, 2)
    assert {(row["daily_limit"], row["rpm_limit"]) for row in copies} == {(7, 2)}


def test_contract_ignores_legacy_account_override_drift(fresh_db):
    _user_id, primary, resident = _user_with_resident("13800024002")
    with db.connect() as conn:
        conn.execute(
            "UPDATE accounts SET daily_limit = 20, rpm_limit = 8 WHERE id = ?", (primary,)
        )
        conn.execute(
            "UPDATE accounts SET daily_limit = 5, rpm_limit = 3 WHERE id = ?", (resident,)
        )

    for account_id in (primary, resident):
        limits = db.resolve_effective_quota_limits(
            account_id=account_id, default_daily=50, default_rpm=10
        )
        assert limits["daily_limit"] == 50
        assert limits["rpm_limit"] == 10
        assert limits["source"] == "product_membership"


def test_clearing_override_restores_shared_defaults(fresh_db):
    _user_id, primary, resident = _user_with_resident("13800024003")
    db.update_account(account_id=primary, daily_limit=7, rpm_limit=2)
    db.update_account(account_id=resident, daily_limit=None, rpm_limit=None)

    for account_id in (primary, resident):
        limits = db.resolve_effective_quota_limits(
            account_id=account_id, default_daily=50, default_rpm=10
        )
        assert limits["daily_limit"] == 50
        assert limits["rpm_limit"] == 10


def test_updating_one_override_preserves_the_other(fresh_db):
    _user_id, primary, resident = _user_with_resident("13800024004")
    db.update_account(account_id=primary, daily_limit=7, rpm_limit=2)
    db.update_account(account_id=resident, daily_limit=9)

    limits = db.resolve_effective_quota_limits(
        account_id=primary, default_daily=50, default_rpm=10
    )
    assert limits["daily_limit"] == 9
    assert limits["rpm_limit"] == 2


def test_m0031_is_idempotent(fresh_db):
    with db.connect() as conn:
        _migration_0031_platform_user_quota_overrides(conn)
        _migration_0031_platform_user_quota_overrides(conn)
        conn.execute("SELECT daily_limit, rpm_limit FROM platform_users LIMIT 1")
