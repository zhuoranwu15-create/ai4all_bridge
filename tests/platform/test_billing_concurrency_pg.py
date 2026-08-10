"""#11 钱包侧 PG 并发硬闸（§9 发布闸）。

验证 D-14 一真人一共享钱包在真并发下的四条正确性不变量：
  1. 跨居民并发扣款（同真人多 account、不同 idempotency_key）→ 余额恰为各笔求和、
     无丢更新（靠 `balance = balance + ?` 原子自增）；cost_events/ledger 各 N 行。
  2. 同 idempotency_key 并发 → 恰扣一次（靠 cost_events/entitlement_ledger 双 UNIQUE）；
     败者抛 IntegrityError、被 `connect()` 回滚半途扣减，不双记、不污染。
  3. 跨真人并发 → 各自钱包互不误伤（账号隔离核心不变量）。
  4. 同真人的微信 binding 账号与世界居民账号并发 → 两条 owner 解析路径汇聚同一钱包，
     余额不丢更新；居民不需要 owner_binding。

并发正确性以 PostgreSQL 为准；功能正确性另由 tests/test_billing_charges.py 覆盖。
"""
import concurrent.futures

import pytest

import app.db as db
from app.bootstrap.product_registry import build_test_product_registry
from app.db._backend import IntegrityError
from app.db.billing import _shell_micros_for_tokens
from tests.factories import make_resident_account, make_user_account

# 每笔扣款固定 token → 固定扣减，便于按笔数断言总额。
_INPUT_TOKENS = 1000
_OUTPUT_TOKENS = 200
_PER_DEBIT = _shell_micros_for_tokens(billable_tokens=_INPUT_TOKENS + _OUTPUT_TOKENS)


def _user_one_account(phone: str):
    user = db.create_or_get_platform_user_by_phone(phone=phone, display_name="并发计费")
    a = db.create_ai4all_account_for_user(
        app_id="zhaoxi",
        platform_user_id=user["id"], display_name="居民"
    )["account"]["id"]
    return user["id"], a


def _user_two_residents(phone: str):
    user = db.create_or_get_platform_user_by_phone(phone=phone, display_name="并发计费多号")
    # 用户注册入口只负责初始化真人钱包；两个实际扣款账号均为 form-B 居民，
    # 经 universe 归属而非 owner_binding 解析到同一真人。
    make_user_account(user["id"], "用户账号", app_id="mingchan")
    a1 = make_resident_account(user["id"], "甲")
    a2 = make_resident_account(user["id"], "乙")
    return user["id"], a1, a2


def _user_binding_and_resident(phone: str):
    user = db.create_or_get_platform_user_by_phone(phone=phone, display_name="混合解析并发计费")
    binding_account = make_user_account(
        user["id"], "鸣蝉用户账号", app_id="mingchan"
    )
    resident_account = make_resident_account(user["id"], "世界居民")
    return user["id"], binding_account, resident_account


def _balance(pu: str) -> int:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT balance_shell_micros FROM entitlement_wallets "
            "WHERE platform_user_id = ? AND status = 'active'",
            (pu,),
        ).fetchone()
    return int(row["balance_shell_micros"]) if row else 0


def _cost_event_count(pu: str) -> int:
    with db.connect() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM cost_events WHERE platform_user_id = ?",
                (pu,),
            ).fetchone()["c"]
        )


def _ledger_debit_count(pu: str) -> int:
    with db.connect() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM entitlement_ledger "
                "WHERE platform_user_id = ? AND entry_type = 'debit'",
                (pu,),
            ).fetchone()["c"]
        )


def _charge(account_id: str, idempotency_key: str, source_id: str):
    return db.record_chat_usage_charge(
        account_id=account_id,
        model="deepseek-chat",
        messages=[{"role": "user", "content": "hi"}],
        reply="ok",
        source_type="chat_turn",
        source_id=source_id,
        idempotency_key=idempotency_key,
        input_tokens=_INPUT_TOKENS,
        output_tokens=_OUTPUT_TOKENS,
        registry=build_test_product_registry(),
    )


# ---------------------------------------------------------------------------
# 1. 跨居民并发扣款：共享钱包，不同 key，不丢更新
# ---------------------------------------------------------------------------
def test_concurrent_cross_resident_charges_no_lost_update(fresh_db):

    pu, a1, a2 = _user_two_residents("13800030001")
    before = _balance(pu)
    accounts = [a1, a2]
    n = 12

    def _task(i: int):
        # 两个居民交替发起，命中同一共享钱包；key 互不相同 → 每笔都应落账。
        return _charge(accounts[i % 2], f"concur-{i}", f"src-{i}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_task, range(n)))

    assert all(r is not None for r in results)  # 每笔都成功
    assert _cost_event_count(pu) == n
    assert _ledger_debit_count(pu) == n
    # 原子自增：余额恰为初始 - 各笔求和，无一笔因并发覆盖丢失。
    assert _balance(pu) == before - _PER_DEBIT * n


# ---------------------------------------------------------------------------
# 2. 同 idempotency_key 并发：恰扣一次（双 UNIQUE 挡下双记）
# ---------------------------------------------------------------------------
def test_concurrent_same_idempotency_key_charges_once(fresh_db):

    pu, a = _user_one_account("13800030002")
    before = _balance(pu)
    key = "dup-key-1"
    n = 8
    conflicts = {"n": 0}

    def _task(_i: int):
        try:
            return _charge(a, key, "src-dup")
        except IntegrityError:
            # 并发败者：唯一约束挡下双记，异常已由 connect() 回滚半途扣减。
            # 真实 turn 路径由 insert_message 去重不可达此竞争；turn_service 亦 try/except。
            conflicts["n"] += 1
            return "conflict"

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        outcomes = list(ex.map(_task, range(n)))

    # 恰好一笔 cost_event + 一条 debit ledger 落库，余额只扣一次。
    with db.connect() as conn:
        ce = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM cost_events WHERE idempotency_key = ?",
                (key,),
            ).fetchone()["c"]
        )
        lg = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM entitlement_ledger WHERE idempotency_key = ?",
                (f"usage-charge-{key}",),
            ).fetchone()["c"]
        )
    assert ce == 1
    assert lg == 1
    assert _balance(pu) == before - _PER_DEBIT  # 只扣一次，不双记
    # 至少一个 worker 成功落账（其余为幂等返回既有或冲突败者）。
    assert any(o not in ("conflict",) and o is not None for o in outcomes)


# ---------------------------------------------------------------------------
# 3. 跨真人并发：各自钱包互不误伤（账号隔离）
# ---------------------------------------------------------------------------
def test_concurrent_charges_across_users_do_not_interfere(fresh_db):

    pu_a, acc_a = _user_one_account("13800030003")
    pu_b, acc_b = _user_one_account("13800030004")
    before_a = _balance(pu_a)
    before_b = _balance(pu_b)
    n = 6  # 偶数 → A，奇数 → B，各 3 笔

    def _task(i: int):
        if i % 2 == 0:
            return _charge(acc_a, f"xuser-a-{i}", f"src-a-{i}")
        return _charge(acc_b, f"xuser-b-{i}", f"src-b-{i}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_task, range(n)))

    assert all(r is not None for r in results)
    assert _balance(pu_a) == before_a - _PER_DEBIT * 3
    assert _balance(pu_b) == before_b - _PER_DEBIT * 3
    assert _cost_event_count(pu_a) == 3
    assert _cost_event_count(pu_b) == 3


# ---------------------------------------------------------------------------
# 4. 微信 binding + 世界居民：两条 owner 解析路径共享钱包，不丢更新
# ---------------------------------------------------------------------------
def test_concurrent_binding_and_world_resident_share_wallet(fresh_db):

    pu, binding_account, resident_account = _user_binding_and_resident("13800030005")
    assert binding_account != resident_account
    with db.connect() as conn:
        # 形态 A 经 owner_binding、形态 B 经 universe 归属，最终必须解析到同一真人。
        assert db.resolve_owner_platform_user_id(conn, binding_account) == pu
        assert db.resolve_owner_platform_user_id(conn, resident_account) == pu
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM account_owner_bindings WHERE account_id = ?",
            (binding_account,),
        ).fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM account_owner_bindings WHERE account_id = ?",
            (resident_account,),
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM entitlement_wallets "
            "WHERE platform_user_id = ? AND status = 'active'",
            (pu,),
        ).fetchone()["c"] == 1

    before = _balance(pu)
    accounts = [binding_account, resident_account]
    n = 12

    def _task(i: int):
        return _charge(accounts[i % 2], f"mixed-owner-{i}", f"mixed-src-{i}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_task, range(n)))

    assert all(r is not None for r in results)
    assert _cost_event_count(pu) == n
    assert _ledger_debit_count(pu) == n
    assert _balance(pu) == before - _PER_DEBIT * n
