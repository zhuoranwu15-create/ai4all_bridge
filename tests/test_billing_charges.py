"""P0-4 计费红线单测：token→贝壳换算、扣费/赠贝幂等、新用户赠送恰好一次、账号隔离。

billing.py 是金钱红线但此前只有黑盒间接覆盖；这里补隔离的单元断言，作为后续重构的
安全网。所有用例走内存 SQLite（fresh_db fixture）。
"""
import pytest

from app.db._core import (
    NEW_USER_GRANT_SHELL_MICROS,
    SHELL_BILLABLE_TOKENS_PER_SHELL,
    SHELL_MICROS_PER_SHELL,
)


def _create_account(*, phone: str = "13800007001") -> str:
    """建账号 + owner binding（使 get_platform_user_id_for_account 返回非空）+ wallet。"""
    import app.db as db

    user = db.create_or_get_platform_user_by_phone(phone=phone, display_name="计费用户")
    bundle = db.get_or_create_default_ai4all_account_for_user(
        app_id="zhaoxi",
        platform_user_id=user["id"], display_name="计费 Bot"
    )
    return bundle["account"]["id"]


def _balance(account_id: str) -> int:
    import app.db as db

    with db.connect() as conn:
        row = conn.execute(
            "SELECT balance_shell_micros FROM entitlement_wallets WHERE account_id=?",
            (account_id,),
        ).fetchone()
    return int(row["balance_shell_micros"]) if row else 0


# ---------- token → 贝壳换算（纯函数，无 DB） ----------
def test_shell_micros_for_tokens_conversion():
    from app.db.billing import _shell_micros_for_tokens

    # 0 / 负数 token 不计费。
    assert _shell_micros_for_tokens(billable_tokens=0) == 0
    assert _shell_micros_for_tokens(billable_tokens=-5) == 0

    # 恰好 1 贝壳额度的 token 数 → 恰好 1 贝壳 micros。
    assert (
        _shell_micros_for_tokens(billable_tokens=SHELL_BILLABLE_TOKENS_PER_SHELL)
        == SHELL_MICROS_PER_SHELL
    )

    # 任意正 token 至少扣到 1 micro（ceil 向上取整，不会被抹成 0）。
    assert _shell_micros_for_tokens(billable_tokens=1) > 0

    # 价格乘数线性放大。
    base = _shell_micros_for_tokens(billable_tokens=1500)
    doubled = _shell_micros_for_tokens(
        billable_tokens=1500, model_price_multiplier_micros=2_000_000
    )
    assert doubled == base * 2


# ---------- 新用户赠送：恰好一次 ----------
def test_grant_new_user_shells_granted_exactly_once(fresh_db):
    import app.db as db

    account_id = _create_account()
    platform_user_id = db.get_platform_user_id_for_account(account_id=account_id)

    # 重复调用（含账号创建期可能已赠送）——幂等键固定，最终只赠送一次。
    db.grant_new_user_shells(account_id=account_id, platform_user_id=platform_user_id)
    db.grant_new_user_shells(account_id=account_id, platform_user_id=platform_user_id)

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT amount_shell_micros
            FROM entitlement_ledger
            WHERE account_id=? AND source_type='new_user_grant'
            """,
            (account_id,),
        ).fetchall()
    assert len(rows) == 1
    assert int(rows[0]["amount_shell_micros"]) == NEW_USER_GRANT_SHELL_MICROS


# ---------- grant_shells：幂等 + 正数校验 ----------
def test_grant_shells_idempotent_and_positive_only(fresh_db):
    import app.db as db

    account_id = _create_account()
    platform_user_id = db.get_platform_user_id_for_account(account_id=account_id)
    before = _balance(account_id)

    def _grant(key: str):
        return db.grant_shells(
            account_id=account_id,
            platform_user_id=platform_user_id,
            amount_shell_micros=3_000_000,
            source_type="manual_test",
            source_id="t1",
            idempotency_key=key,
        )

    _grant("grant-k1")
    after_first = _balance(account_id)
    assert after_first == before + 3_000_000

    # 同 idempotency_key 重放，不重复入账。
    _grant("grant-k1")
    assert _balance(account_id) == after_first

    # 不同 key 再次入账。
    _grant("grant-k2")
    assert _balance(account_id) == after_first + 3_000_000

    # 非正金额拒绝。
    with pytest.raises(ValueError):
        db.grant_shells(
            account_id=account_id,
            platform_user_id=platform_user_id,
            amount_shell_micros=0,
            source_type="manual_test",
            source_id="t1",
            idempotency_key="grant-zero",
        )


# ---------- record_chat_usage_charge：扣费正确 + 幂等 + 无 owner 返回 None ----------
def test_record_chat_usage_charge_debits_and_is_idempotent(fresh_db):
    import app.db as db
    from app.db.billing import _shell_micros_for_tokens

    account_id = _create_account()
    before = _balance(account_id)

    expected_debit = _shell_micros_for_tokens(billable_tokens=2000 + 500)

    first = db.record_chat_usage_charge(
        account_id=account_id,
        model="deepseek-chat",
        messages=[{"role": "user", "content": "hi"}],
        reply="hello",
        source_type="chat",
        source_id="msg-1",
        idempotency_key="usage-k1",
        input_tokens=2000,
        output_tokens=500,
    )
    assert first is not None
    assert first["cost_event"]["computed_shell_micros"] == expected_debit
    # 提供了显式 token，应标记为非估算。
    assert first["cost_event"]["metadata"]["estimated"] is False
    after_first = _balance(account_id)
    assert after_first == before - expected_debit

    # 同 idempotency_key 重放：余额不变、cost_event 不新增。
    second = db.record_chat_usage_charge(
        account_id=account_id,
        model="deepseek-chat",
        messages=[{"role": "user", "content": "hi"}],
        reply="hello",
        source_type="chat",
        source_id="msg-1",
        idempotency_key="usage-k1",
        input_tokens=2000,
        output_tokens=500,
    )
    assert second is not None
    assert _balance(account_id) == after_first
    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) c FROM cost_events WHERE idempotency_key='usage-k1'",
        ).fetchone()["c"]
    assert count == 1


def test_record_chat_usage_charge_returns_none_without_owner(fresh_db):
    import app.db as db

    # 无 owner binding 的账号：即使 token>0，也不扣费（返回 None），不污染他人钱包。
    result = db.record_chat_usage_charge(
        account_id="ghost-account-no-owner",
        model="deepseek-chat",
        messages=[{"role": "user", "content": "x"}],
        reply="y",
        source_type="chat",
        source_id="m",
        idempotency_key="ghost-k1",
        input_tokens=1000,
        output_tokens=1000,
    )
    assert result is None
