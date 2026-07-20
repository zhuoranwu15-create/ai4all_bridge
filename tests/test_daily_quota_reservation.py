"""D-09 下半刀：daily 配额原子预占 / 确认 / 回滚 / TTL 的直接单测。

覆盖 accounts.reserve_daily_quota / confirm_daily_quota / rollback_daily_quota /
reclaim_expired_reservations。功能用例走 fresh_db（SQLite 默认，PG 档亦跑）；并发不超卖
用例仅在 PG 下算数（§9 硬门禁：SQLite 单写者天然串行，无法复现 advisory 锁语义），SQLite
下 skip。
"""
import concurrent.futures

import pytest

import app.db as db
from app.db._backend import is_postgres

_DATE = "2026-07-19"


def _user_with_account(phone: str):
    user = db.create_or_get_platform_user_by_phone(phone=phone, display_name="预占用户")
    a = db.create_ai4all_account_for_user(
        platform_user_id=user["id"], display_name="居民"
    )["account"]["id"]
    return user["id"], a


def _two_accounts_one_user(phone: str):
    user = db.create_or_get_platform_user_by_phone(phone=phone, display_name="多号预占用户")
    a1 = db.create_ai4all_account_for_user(
        platform_user_id=user["id"], display_name="甲"
    )["account"]["id"]
    a2 = db.create_ai4all_account_for_user(
        platform_user_id=user["id"], display_name="乙"
    )["account"]["id"]
    return user["id"], a1, a2


def _count_reservations(pu: str) -> int:
    with db.connect() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM daily_quota_reservations WHERE platform_user_id = ?",
                (pu,),
            ).fetchone()["c"]
        )


# ---------------------------------------------------------------------------
# 功能：reserve → confirm 计入 message_count；reserve 满则拒
# ---------------------------------------------------------------------------
def test_reserve_then_confirm_increments_message_count(fresh_db):
    _pu, a = _user_with_account("13800020001")
    token = db.reserve_daily_quota(account_id=a, date=_DATE, limit=5)
    assert token
    # 预占阶段 message_count 未变（展示口径 = 已确认）。
    assert db.get_daily_usage(account_id=a, date=_DATE) == 0
    db.confirm_daily_quota(reservation_id=token)
    assert db.get_daily_usage(account_id=a, date=_DATE) == 1


def test_reserve_rejects_when_full(fresh_db):
    pu, a = _user_with_account("13800020002")
    t1 = db.reserve_daily_quota(account_id=a, date=_DATE, limit=2)
    t2 = db.reserve_daily_quota(account_id=a, date=_DATE, limit=2)
    t3 = db.reserve_daily_quota(account_id=a, date=_DATE, limit=2)
    assert t1 and t2
    assert t3 is None  # message_count(0) + reserved(2) >= 2 → 拒
    assert _count_reservations(pu) == 2


def test_confirm_is_idempotent(fresh_db):
    _pu, a = _user_with_account("13800020003")
    token = db.reserve_daily_quota(account_id=a, date=_DATE, limit=5)
    db.confirm_daily_quota(reservation_id=token)
    db.confirm_daily_quota(reservation_id=token)  # 二次确认：行已删 → no-op、不双记
    assert db.get_daily_usage(account_id=a, date=_DATE) == 1


def test_rollback_frees_slot_and_keeps_count(fresh_db):
    pu, a = _user_with_account("13800020004")
    t1 = db.reserve_daily_quota(account_id=a, date=_DATE, limit=1)
    assert t1
    # 满了（reserved=1 == limit）
    assert db.reserve_daily_quota(account_id=a, date=_DATE, limit=1) is None
    db.rollback_daily_quota(reservation_id=t1)
    assert db.get_daily_usage(account_id=a, date=_DATE) == 0  # 回滚不计数
    assert _count_reservations(pu) == 0
    # 名额已释放，可再预占
    assert db.reserve_daily_quota(account_id=a, date=_DATE, limit=1)


def test_rollback_is_idempotent_and_none_is_noop(fresh_db):
    _pu, a = _user_with_account("13800020005")
    token = db.reserve_daily_quota(account_id=a, date=_DATE, limit=3)
    db.rollback_daily_quota(reservation_id=token)
    db.rollback_daily_quota(reservation_id=token)  # 删缺失 = no-op
    db.rollback_daily_quota(reservation_id=None)  # None → no-op
    db.confirm_daily_quota(reservation_id=None)  # None → no-op
    assert db.get_daily_usage(account_id=a, date=_DATE) == 0


def test_limit_non_positive_is_unlimited_but_still_counts(fresh_db):
    """limit<=0 视为不限流：必得 token、不拒；确认仍计入 message_count（保留展示语义）。"""
    _pu, a = _user_with_account("13800020006")
    for _ in range(3):
        token = db.reserve_daily_quota(account_id=a, date=_DATE, limit=0)
        assert token
        db.confirm_daily_quota(reservation_id=token)
    assert db.get_daily_usage(account_id=a, date=_DATE) == 3


def test_confirmed_plus_reserved_enforced(fresh_db):
    """强制口径 = 已确认 message_count + 在途 reservation 数。"""
    _pu, a = _user_with_account("13800020007")
    t1 = db.reserve_daily_quota(account_id=a, date=_DATE, limit=2)
    db.confirm_daily_quota(reservation_id=t1)  # message_count=1
    t2 = db.reserve_daily_quota(account_id=a, date=_DATE, limit=2)  # used1+reserved1=2? no: used1+reserved0=1<2 → ok
    assert t2
    # 此刻 used=1, reserved=1 → 满
    assert db.reserve_daily_quota(account_id=a, date=_DATE, limit=2) is None


# ---------------------------------------------------------------------------
# TTL：prune-on-touch 释放过期预占 + reclaim 批量回收
# ---------------------------------------------------------------------------
def _expire_all_reservations():
    """把所有 reservation 的 expires_at 回填到过去（模拟崩溃悬挂 + TTL 到期）。"""
    with db.connect() as conn:
        conn.execute(
            "UPDATE daily_quota_reservations SET expires_at = '2000-01-01 00:00:00'"
        )


def test_ttl_prune_on_touch_frees_expired_slot(fresh_db):
    _pu, a = _user_with_account("13800020008")
    assert db.reserve_daily_quota(account_id=a, date=_DATE, limit=1)
    assert db.reserve_daily_quota(account_id=a, date=_DATE, limit=1) is None  # 满
    _expire_all_reservations()
    # 下次 reserve 触发 prune-on-touch，过期悬挂被清、名额释放。
    assert db.reserve_daily_quota(account_id=a, date=_DATE, limit=1)


def test_reclaim_expired_reservations(fresh_db):
    pu, a = _user_with_account("13800020009")
    db.reserve_daily_quota(account_id=a, date=_DATE, limit=5)
    db.reserve_daily_quota(account_id=a, date=_DATE, limit=5)
    assert _count_reservations(pu) == 2
    _expire_all_reservations()
    reclaimed = db.reclaim_expired_reservations(now="2026-07-19 00:00:00")
    assert reclaimed == 2
    assert _count_reservations(pu) == 0
    # 未过期的不被清
    db.reserve_daily_quota(account_id=a, date=_DATE, limit=5)
    assert db.reclaim_expired_reservations(now="2000-01-01 00:00:00") == 0
    assert _count_reservations(pu) == 1


# ---------------------------------------------------------------------------
# 一人多号：共享一套配额（跨居民）
# ---------------------------------------------------------------------------
def test_multi_account_one_user_shares_reservation_quota(fresh_db):
    pu, a1, a2 = _two_accounts_one_user("13800020010")
    t1 = db.reserve_daily_quota(account_id=a1, date=_DATE, limit=2)
    t2 = db.reserve_daily_quota(account_id=a2, date=_DATE, limit=2)
    assert t1 and t2
    # 第三次（任一号）撞共享上限。
    assert db.reserve_daily_quota(account_id=a1, date=_DATE, limit=2) is None
    db.confirm_daily_quota(reservation_id=t1)
    db.confirm_daily_quota(reservation_id=t2)
    # 两号读到同一份共享计数。
    assert db.get_daily_usage(account_id=a1, date=_DATE) == 2
    assert db.get_daily_usage(account_id=a2, date=_DATE) == 2


def test_rollback_does_not_touch_other_users_quota(fresh_db):
    """回滚只动自己的 reservation，不误伤他人配额（D-09 L108）。"""
    pu_a, acc_a = _user_with_account("13800020011")
    pu_b, acc_b = _user_with_account("13800020012")
    ta = db.reserve_daily_quota(account_id=acc_a, date=_DATE, limit=3)
    tb = db.reserve_daily_quota(account_id=acc_b, date=_DATE, limit=3)
    db.rollback_daily_quota(reservation_id=ta)
    assert _count_reservations(pu_a) == 0
    assert _count_reservations(pu_b) == 1  # B 不受影响
    db.confirm_daily_quota(reservation_id=tb)
    assert db.get_daily_usage(account_id=acc_b, date=_DATE) == 1
    assert db.get_daily_usage(account_id=acc_a, date=_DATE) == 0


# ---------------------------------------------------------------------------
# PG 并发不超卖（§9 硬门禁；SQLite skip）
# ---------------------------------------------------------------------------
def test_concurrent_reserve_no_oversell_pg(fresh_db):
    """跨居民/并发同一真人抢预占：advisory 锁串行，恰好 limit 个成功、不超卖。

    仅 PG 算数：SQLite 单写者天然串行，无法复现 READ COMMITTED 下的 TOCTOU 击穿。
    """
    if not is_postgres():
        pytest.skip("并发不超卖只在 PG 档算数（§9 硬门禁，SQLite 绿不作数）")

    pu, a = _user_with_account("13800020013")
    limit = 3
    workers = 8

    def _try_reserve(_i):
        return db.reserve_daily_quota(account_id=a, date=_DATE, limit=limit)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        tokens = list(ex.map(_try_reserve, range(workers)))

    granted = [t for t in tokens if t]
    assert len(granted) == limit  # 恰好 limit 个成功
    assert _count_reservations(pu) == limit  # 落库行数 = limit，不超卖
