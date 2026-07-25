"""MP-02 wipe 只清理当前产品的共享计费资产。"""
import app.db as db
from app.bootstrap.product_registry import build_test_product_registry
from app.db._core import _referral_contract_violation_counts


def _two_product_accounts(phone: str):
    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone=phone)
    zhaoxi = db.create_ai4all_account_for_user(
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


def _asset_counts(conn, *, user_id: str):
    wallets = {
        row["app_id"]: int(row["n"])
        for row in conn.execute(
            """
            SELECT app_id, COUNT(*) AS n FROM entitlement_wallets
            WHERE platform_user_id=? GROUP BY app_id
            """,
            (user_id,),
        ).fetchall()
    }
    ledger = {
        row["app_id"]: int(row["n"])
        for row in conn.execute(
            """
            SELECT app_id, COUNT(*) AS n FROM entitlement_ledger
            WHERE platform_user_id=? GROUP BY app_id
            """,
            (user_id,),
        ).fetchall()
    }
    subscriptions = {
        row["app_id"]: int(row["n"])
        for row in conn.execute(
            """
            SELECT app_id, COUNT(*) AS n FROM subscriptions
            WHERE platform_user_id=? GROUP BY app_id
            """,
            (user_id,),
        ).fetchall()
    }
    return wallets, ledger, subscriptions


def test_wipe_zhaoxi_ignores_other_product_account_and_preserves_its_assets(fresh_db):
    registry, user_id, zhaoxi_id, test_id = _two_product_accounts("13800037501")
    db.increment_daily_usage(account_id=zhaoxi_id, date="2026-07-24")

    stats = db.wipe_account_data(account_id=zhaoxi_id)

    assert stats["entitlement_wallets_deleted"] == 1
    assert stats["entitlement_ledger_deleted"] == 1
    assert stats["daily_usage_deleted"] == 1
    with db.connect() as conn:
        wallets, ledger, subscriptions = _asset_counts(conn, user_id=user_id)
    assert wallets == {"test_product": 1}
    assert ledger == {"test_product": 1}
    # Subscription 是 membership 级状态历史，account wipe 不删除任一产品历史。
    assert subscriptions == {"test_product": 1, "zhaoxi": 1}
    assert db.get_wallet_summary(
        account_id=test_id, create_if_missing=False, registry=registry
    )["wallet"]["app_id"] == "test_product"


def test_wipe_test_product_never_deletes_legacy_zhaoxi_daily_or_assets(fresh_db):
    registry, user_id, zhaoxi_id, test_id = _two_product_accounts("13800037502")
    db.increment_daily_usage(account_id=zhaoxi_id, date="2026-07-24")

    stats = db.wipe_account_data(account_id=test_id)

    assert stats["entitlement_wallets_deleted"] == 1
    assert stats["entitlement_ledger_deleted"] == 1
    assert stats["daily_usage_deleted"] == 0
    with db.connect() as conn:
        wallets, ledger, subscriptions = _asset_counts(conn, user_id=user_id)
        daily = conn.execute(
            "SELECT COUNT(*) AS n FROM daily_usage WHERE platform_user_id=?",
            (user_id,),
        ).fetchone()["n"]
    assert wallets == {"zhaoxi": 1}
    assert ledger == {"zhaoxi": 1}
    assert subscriptions == {"test_product": 1, "zhaoxi": 1}
    assert int(daily) == 1
    assert db.get_wallet_summary(
        account_id=zhaoxi_id, create_if_missing=False
    )["wallet"]["app_id"] == "zhaoxi"


def test_wipe_preserves_wallet_audit_chain_referenced_by_referral_reward(fresh_db):
    inviter = db.create_or_get_platform_user_by_phone(phone="13800037503")
    inviter_account = db.create_ai4all_account_for_user(
        platform_user_id=inviter["id"], display_name="推荐邀请人"
    )["account"]["id"]
    code = db.get_or_create_personal_referral_code_for_user(
        platform_user_id=inviter["id"]
    )
    invitee = db.register_platform_user_with_referral(
        phone="13800037504", invite_code=code["code"]
    )["platform_user"]
    invitee_account = db.create_ai4all_account_for_user(
        platform_user_id=invitee["id"], display_name="推荐被邀请人"
    )["account"]["id"]
    session = db.get_or_create_session(
        account_id=invitee_account,
        channel="native",
        sender_id="referral-invitee",
        sender_name=None,
        chat_id="referral-invitee",
        session_key="referral-invitee",
    )["session"]
    result = None
    for index in range(3):
        message_id = db.insert_message(
            account_id=invitee_account,
            session_id=session["id"],
            message_id=f"wipe-referral-{index}",
            reply_to_message_id=None,
            direction="inbound",
            role="user",
            message_type="text",
            content=f"这是第 {index + 1} 条真实且有意义的邀请互动消息",
        )
        result = db.process_referral_message_for_account(
            account_id=invitee_account, message_db_id=message_id
        )
    assert result is not None
    reward_ledger_id = result["relationship"]["reward_ledger_id"]
    assert reward_ledger_id is not None
    charge = db.record_chat_usage_charge(
        account_id=inviter_account,
        model="test-model",
        messages=[{"role": "user", "content": "保留审计成本"}],
        reply="已记录",
        source_type="turn",
        source_id="wipe-referral-cost",
        idempotency_key="wipe-referral-cost",
        input_tokens=10,
        output_tokens=10,
    )
    assert charge is not None

    stats = db.wipe_account_data(account_id=inviter_account)

    assert stats["entitlement_wallets_deleted"] == 0
    assert stats["entitlement_ledger_deleted"] == 0
    assert stats["cost_events_deleted"] == 0
    with db.connect() as conn:
        relationship = conn.execute(
            """
            SELECT reward_ledger_id FROM referral_relationships
            WHERE invitee_platform_user_id=? AND app_id='zhaoxi'
            """,
            (invitee["id"],),
        ).fetchone()
        reward = conn.execute(
            "SELECT wallet_id FROM entitlement_ledger WHERE id=?",
            (reward_ledger_id,),
        ).fetchone()
        wallet = conn.execute(
            "SELECT id FROM entitlement_wallets WHERE id=?",
            (reward["wallet_id"],),
        ).fetchone()
        costs = conn.execute(
            "SELECT COUNT(*) AS n FROM cost_events WHERE wallet_id=?",
            (reward["wallet_id"],),
        ).fetchone()["n"]
        violations = _referral_contract_violation_counts(conn)
    assert relationship["reward_ledger_id"] == reward_ledger_id
    assert wallet is not None and int(costs) == 1
    assert all(count == 0 for count in violations.values())
