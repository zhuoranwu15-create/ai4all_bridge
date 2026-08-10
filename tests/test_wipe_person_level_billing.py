"""① wipe_account_data 真人级钱包/daily 保留-删除双分支（D-14/D-09 M1，codex finding ①）。

同真人多号共享一钱包 + 一套 daily 配额；wipe 单号若误按 account_id 删这两张真人级表，会丢同真人
其他号的余额/配额（PG 无 FK→静默丢失；SQLite FK→其他号 ledger 悬挂致 IntegrityError 整体回滚）。
验证：wipe 非末号 → 共享钱包/daily 原样保留且**不抛**；wipe 末号 → 真正拆除。走 fresh_db。
"""
import app.db as db
from app.bootstrap.product_registry import build_test_product_registry
from tests.factories import make_resident_account, make_user_account

_DATE = "2026-07-21"


def _active_wallet(conn, platform_user_id):
    return conn.execute(
        "SELECT id, balance_shell_micros, status FROM entitlement_wallets "
        "WHERE platform_user_id = ? AND status = 'active'",
        (platform_user_id,),
    ).fetchone()


def test_wipe_preserves_shared_wallet_when_person_has_other_accounts(fresh_db):
    """同真人尚有其他 active 号：wipe 本号不得删共享钱包/daily、不得抛。"""
    pu = db.create_or_get_platform_user_by_phone(
        phone="13900040001", display_name="共享钱包"
    )["id"]
    # 决策 B：a1 = 用户账号（form-A，发 owner_binding）；a2 = 居民（form-B，无 binding，经世界归属
    # 共享真人钱包/daily）。wipe a1（非末号）的 sibling 检查须能经世界归属看见 a2。
    a1 = make_user_account(pu, "甲", app_id="mingchan")
    a2 = make_resident_account(pu, "乙")

    # a2 追加一笔手工赠权 → 生成 account_id=a2、引用共享钱包的 ledger 行（正是旧代码删钱包时
    # 触发 SQLite FK 崩 / PG 悬挂的那类跨号引用）。
    db.grant_shells(
        account_id=a2, platform_user_id=pu, amount_shell_micros=1_000_000,
        source_type="manual_grant", source_id="t", idempotency_key="wipe-shared-extra",
        registry=build_test_product_registry(),
    )
    db.increment_daily_usage(account_id=a1, date=_DATE)
    db.increment_daily_usage(account_id=a2, date=_DATE)  # 共享同一 (真人,date) 行

    with db.connect() as conn:
        before = _active_wallet(conn, pu)
    assert before is not None
    balance_before = int(before["balance_shell_micros"])

    # wipe a1（非末号）：不抛；共享钱包/daily 保留（stats 计 0）。
    stats = db.wipe_account_data(account_id=a1)
    assert stats["entitlement_wallets_deleted"] == 0
    assert stats["daily_usage_deleted"] == 0

    with db.connect() as conn:
        after = _active_wallet(conn, pu)
    assert after is not None and after["id"] == before["id"]
    assert int(after["balance_shell_micros"]) == balance_before  # a1 wipe 不动共享余额
    # a2 仍按真人解析到共享钱包 + 共享 daily 计数。
    assert db.get_daily_usage(account_id=a2, date=_DATE) == 2


def test_wipe_last_account_tears_down_person_wallet(fresh_db):
    """真人末号（无其他 active 号）：wipe 真正拆除真人级钱包 + daily。"""
    pu = db.create_or_get_platform_user_by_phone(
        phone="13900040002", display_name="末号拆除"
    )["id"]
    a1 = db.create_ai4all_account_for_user(
        platform_user_id=pu,
        display_name="独",
        app_id="zhaoxi",
    )["account"]["id"]
    db.increment_daily_usage(account_id=a1, date=_DATE)

    stats = db.wipe_account_data(account_id=a1)
    assert stats["entitlement_wallets_deleted"] == 1
    assert stats["daily_usage_deleted"] == 1
    with db.connect() as conn:
        assert _active_wallet(conn, pu) is None
        remaining_daily = conn.execute(
            "SELECT COUNT(*) AS c FROM daily_usage WHERE platform_user_id = ?", (pu,)
        ).fetchone()["c"]
    assert int(remaining_daily) == 0
