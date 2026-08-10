"""MP-04 邀请码、关系、审核与奖励的产品隔离。"""
import concurrent.futures
import threading

import pytest

import app.db as db
from app.bootstrap.product_registry import build_test_product_registry
from app.db._core import (
    _migration_0043_referral_app_id_expand,
    _migration_0044_referral_app_id_contract,
    _referral_contract_violation_counts,
)


def _inviter_with_two_product_codes(phone: str):
    registry = build_test_product_registry()
    inviter = db.create_or_get_platform_user_by_phone(phone=phone)
    zhaoxi_account = db.create_ai4all_account_for_user(
        app_id="zhaoxi",
        platform_user_id=inviter["id"], display_name="朝夕邀请人"
    )["account"]["id"]
    db.ensure_product_membership(
        platform_user_id=inviter["id"], app_id="test_product", registry=registry
    )
    test_account = db.create_ai4all_account_for_user(
        platform_user_id=inviter["id"],
        display_name="测试产品邀请人",
        app_id="test_product",
        registry=registry,
    )["account"]["id"]
    zhaoxi_code = db.get_or_create_personal_referral_code_for_user(
        app_id="zhaoxi",
        platform_user_id=inviter["id"]
    )
    test_code = db.get_or_create_personal_referral_code_for_user(
        platform_user_id=inviter["id"],
        app_id="test_product",
        registry=registry,
    )
    return (
        registry,
        inviter["id"],
        zhaoxi_account,
        test_account,
        zhaoxi_code,
        test_code,
    )


def _register_invitee_in_both_products(*, phone: str, registry, zhaoxi_code, test_code):
    zhaoxi = db.register_platform_user_with_referral(
        phone=phone,
        invite_code=zhaoxi_code["code"],
        app_id="zhaoxi",
        registry=registry,
    )
    test_product = db.register_platform_user_with_referral(
        phone=phone,
        invite_code=test_code["code"],
        app_id="test_product",
        registry=registry,
    )
    return zhaoxi, test_product


def _create_messages_and_process(*, account_id: str, prefix: str, registry, count: int = 3):
    session = db.get_or_create_session(
        account_id=account_id,
        channel="native",
        sender_id=f"sender-{prefix}",
        sender_name=None,
        chat_id=f"chat-{prefix}",
        session_key=f"session-{prefix}",
    )
    result = None
    for index in range(count):
        message_id = db.insert_message(
            account_id=account_id,
            session_id=session["session"]["id"],
            message_id=f"{prefix}-{index}",
            reply_to_message_id=None,
            direction="inbound",
            role="user",
            message_type="text",
            content=f"这是第 {index + 1} 条真实的产品内邀请互动消息",
        )
        result = db.process_referral_message_for_account(
            account_id=account_id,
            message_db_id=message_id,
            registry=registry,
        )
    return result


def test_membership_first_join_consumes_each_product_code_once(fresh_db):
    registry, inviter_id, _za, _ta, zcode, tcode = _inviter_with_two_product_codes(
        "13800037701"
    )

    assert not db.validate_referral_code(
        code=zcode["code"], expected_app_id="test_product", registry=registry
    )["valid"]
    assert not db.validate_referral_code(
        code=tcode["code"], expected_app_id="zhaoxi", registry=registry
    )["valid"]

    zhaoxi, test_product = _register_invitee_in_both_products(
        phone="13800037702",
        registry=registry,
        zhaoxi_code=zcode,
        test_code=tcode,
    )
    invitee_id = zhaoxi["platform_user"]["id"]
    assert zhaoxi["is_new_user"] is True
    assert zhaoxi["is_new_membership"] is True
    assert test_product["is_new_user"] is False
    assert test_product["is_new_membership"] is True
    assert test_product["platform_user"]["id"] == invitee_id

    repeated = db.register_platform_user_with_referral(
        phone="13800037702",
        invite_code="INVALID-BUT-IGNORED-FOR-EXISTING-MEMBERSHIP",
        app_id="test_product",
        registry=registry,
    )
    assert repeated["is_new_membership"] is False
    assert repeated["referral_relationship"] is None

    with db.connect() as conn:
        codes = {
            row["app_id"]: int(row["used_count"])
            for row in conn.execute(
                """
                SELECT app_id, used_count FROM referral_codes
                WHERE platform_user_id=?
                """,
                (inviter_id,),
            ).fetchall()
        }
        relationships = conn.execute(
            """
            SELECT app_id FROM referral_relationships
            WHERE invitee_platform_user_id=? ORDER BY app_id
            """,
            (invitee_id,),
        ).fetchall()
    assert codes == {"test_product": 1, "zhaoxi": 1}
    assert [row["app_id"] for row in relationships] == ["test_product", "zhaoxi"]


def test_meaningful_reviews_and_rewards_are_product_isolated(fresh_db):
    registry, inviter_id, z_inviter, t_inviter, zcode, tcode = (
        _inviter_with_two_product_codes("13800037703")
    )
    zhaoxi, _test_product = _register_invitee_in_both_products(
        phone="13800037704",
        registry=registry,
        zhaoxi_code=zcode,
        test_code=tcode,
    )
    invitee_id = zhaoxi["platform_user"]["id"]
    z_invitee = db.create_ai4all_account_for_user(
        app_id="zhaoxi",
        platform_user_id=invitee_id, display_name="朝夕被邀请人"
    )["account"]["id"]
    t_invitee = db.create_ai4all_account_for_user(
        platform_user_id=invitee_id,
        display_name="测试产品被邀请人",
        app_id="test_product",
        registry=registry,
    )["account"]["id"]

    z_result = _create_messages_and_process(
        account_id=z_invitee, prefix="zhaoxi-ref", registry=registry
    )
    assert z_result["relationship"]["app_id"] == "zhaoxi"
    assert z_result["review"]["app_id"] == "zhaoxi"
    assert z_result["ledger"]["app_id"] == "zhaoxi"
    assert (
        db.get_wallet_summary(account_id=z_inviter, create_if_missing=False)["wallet"]
        ["balance_shell_micros"]
        == 2_000_000_000
    )
    assert (
        db.get_wallet_summary(
            account_id=t_inviter,
            create_if_missing=False,
            registry=registry,
        )["wallet"]["balance_shell_micros"]
        == 1_000_000_000
    )

    t_result = _create_messages_and_process(
        account_id=t_invitee, prefix="test-ref", registry=registry
    )
    assert t_result["relationship"]["app_id"] == "test_product"
    assert t_result["review"]["app_id"] == "test_product"
    assert t_result["ledger"]["app_id"] == "test_product"
    assert (
        db.get_wallet_summary(
            account_id=t_inviter,
            create_if_missing=False,
            registry=registry,
        )["wallet"]["balance_shell_micros"]
        == 2_000_000_000
    )

    z_rows = db.list_referral_relationships(
        inviter_platform_user_id=inviter_id, app_id="zhaoxi", registry=registry
    )
    test_rows = db.list_referral_relationships(
        inviter_platform_user_id=inviter_id,
        app_id="test_product",
        registry=registry,
    )
    assert {row["app_id"] for row in z_rows} == {"zhaoxi"}
    assert {row["app_id"] for row in test_rows} == {"test_product"}


def test_m0043_realigns_relationship_and_review_from_authoritative_parents(fresh_db):
    registry, _inviter_id, _za, _ta, zcode, _tcode = _inviter_with_two_product_codes(
        "13800037711"
    )
    invitee = db.register_platform_user_with_referral(
        phone="13800037712",
        invite_code=zcode["code"],
        app_id="zhaoxi",
        registry=registry,
    )["platform_user"]
    account_id = db.create_ai4all_account_for_user(
        app_id="zhaoxi",
        platform_user_id=invitee["id"], display_name="expand 回填被邀请人"
    )["account"]["id"]
    _create_messages_and_process(
        account_id=account_id, prefix="expand-ref", registry=registry
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE referral_relationships SET app_id='test_product' WHERE invitee_platform_user_id=?",
            (invitee["id"],),
        )
        conn.execute(
            "UPDATE meaningful_message_reviews SET app_id='test_product' WHERE invitee_platform_user_id=?",
            (invitee["id"],),
        )
        _migration_0043_referral_app_id_expand(conn)
        relationship_app = conn.execute(
            "SELECT app_id FROM referral_relationships WHERE invitee_platform_user_id=?",
            (invitee["id"],),
        ).fetchone()["app_id"]
        review_app = conn.execute(
            "SELECT app_id FROM meaningful_message_reviews WHERE invitee_platform_user_id=?",
            (invitee["id"],),
        ).fetchone()["app_id"]
    assert relationship_app == "zhaoxi"
    assert review_app == "zhaoxi"


def test_referral_contract_rejects_review_scope_drift(fresh_db):
    registry, _inviter_id, _za, _ta, zcode, tcode = _inviter_with_two_product_codes(
        "13800037707"
    )
    zhaoxi, _test_product = _register_invitee_in_both_products(
        phone="13800037708",
        registry=registry,
        zhaoxi_code=zcode,
        test_code=tcode,
    )
    invitee_id = zhaoxi["platform_user"]["id"]
    z_account = db.create_ai4all_account_for_user(
        app_id="zhaoxi",
        platform_user_id=invitee_id, display_name="漂移被邀请人"
    )["account"]["id"]
    _create_messages_and_process(
        account_id=z_account, prefix="drift-ref", registry=registry
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE meaningful_message_reviews SET app_id='test_product' WHERE app_id='zhaoxi'"
        )
        counts = _referral_contract_violation_counts(conn)
        assert counts["meaningful_review_scope_drift"] == 1
        with pytest.raises(RuntimeError, match="m0044 referral reconcile failed"):
            _migration_0044_referral_app_id_contract(conn)


def test_pg_concurrent_first_membership_and_reward_are_idempotent(fresh_db):
    registry, inviter_id, _za, _ta, _zcode, tcode = _inviter_with_two_product_codes(
        "13800037709"
    )
    invitee = db.create_or_get_platform_user_by_phone(phone="13800037710")

    def register(_index: int):
        return db.register_platform_user_with_referral(
            phone="13800037710",
            invite_code=tcode["code"],
            app_id="test_product",
            registry=registry,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(register, range(2)))
    assert sum(int(result["is_new_membership"]) for result in results) == 1
    with db.connect() as conn:
        relationship = conn.execute(
            """
            SELECT id FROM referral_relationships
            WHERE invitee_platform_user_id=? AND app_id='test_product'
            """,
            (invitee["id"],),
        ).fetchone()
        used_count = conn.execute(
            "SELECT used_count FROM referral_codes WHERE id=?", (tcode["id"],)
        ).fetchone()["used_count"]
        conn.execute(
            """
            UPDATE referral_relationships
            SET status='qualified', review_status='passed'
            WHERE id=? AND app_id='test_product'
            """,
            (relationship["id"],),
        )
    assert int(used_count) == 1

    def release(_index: int):
        return db.retry_qualified_referral_rewards_for_user(
            platform_user_id=inviter_id,
            app_id="test_product",
            registry=registry,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        released = list(executor.map(release, range(2)))
    assert sum(len(rows) for rows in released) == 1
    with db.connect() as conn:
        rewards = conn.execute(
            """
            SELECT COUNT(*) AS n FROM entitlement_ledger
            WHERE app_id='test_product' AND source_type='referral_reward'
            """
        ).fetchone()["n"]
        relationship_row = conn.execute(
            "SELECT reward_ledger_id FROM referral_relationships WHERE id=?",
            (relationship["id"],),
        ).fetchone()
    assert int(rewards) == 1
    assert relationship_row["reward_ledger_id"] is not None


def test_pg_concurrent_full_registration_for_new_phone_is_idempotent(fresh_db):
    registry, _inviter_id, _za, _ta, _zcode, tcode = _inviter_with_two_product_codes(
        "13800037713"
    )
    barrier = threading.Barrier(2)

    def register(_index: int):
        barrier.wait()
        return db.register_platform_user_with_referral(
            phone="13800037714",
            invite_code=tcode["code"],
            app_id="test_product",
            registry=registry,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(register, range(2)))

    assert len({result["platform_user"]["id"] for result in results}) == 1
    assert sum(int(result["is_new_user"]) for result in results) == 1
    assert sum(int(result["is_new_membership"]) for result in results) == 1
    user_id = results[0]["platform_user"]["id"]
    with db.connect() as conn:
        users = conn.execute(
            "SELECT COUNT(*) AS n FROM platform_users WHERE phone='13800037714'"
        ).fetchone()["n"]
        memberships = conn.execute(
            """
            SELECT COUNT(*) AS n FROM product_memberships
            WHERE platform_user_id=? AND app_id='test_product'
            """,
            (user_id,),
        ).fetchone()["n"]
        relationships = conn.execute(
            """
            SELECT COUNT(*) AS n FROM referral_relationships
            WHERE invitee_platform_user_id=? AND app_id='test_product'
            """,
            (user_id,),
        ).fetchone()["n"]
        used_count = conn.execute(
            "SELECT used_count FROM referral_codes WHERE id=?", (tcode["id"],)
        ).fetchone()["used_count"]
    assert int(users) == 1
    assert int(memberships) == 1
    assert int(relationships) == 1
    assert int(used_count) == 1


def test_pg_concurrent_referral_message_processing_counts_and_rewards_once(fresh_db):
    registry, inviter_id, _za, _ta, _zcode, tcode = _inviter_with_two_product_codes(
        "13800037715"
    )
    invitee = db.register_platform_user_with_referral(
        phone="13800037716",
        invite_code=tcode["code"],
        app_id="test_product",
        registry=registry,
    )["platform_user"]
    account_id = db.create_ai4all_account_for_user(
        platform_user_id=invitee["id"],
        display_name="并发主路径被邀请人",
        app_id="test_product",
        registry=registry,
    )["account"]["id"]
    session = db.get_or_create_session(
        account_id=account_id,
        channel="native",
        sender_id="sender-concurrent-main",
        sender_name=None,
        chat_id="chat-concurrent-main",
        session_key="session-concurrent-main",
    )["session"]
    message_ids = [
        db.insert_message(
            account_id=account_id,
            session_id=session["id"],
            message_id=f"concurrent-main-{index}",
            reply_to_message_id=None,
            direction="inbound",
            role="user",
            message_type="text",
            content=f"这是并发进入的第 {index + 1} 条真实邀请互动消息",
        )
        for index in range(3)
    ]
    barrier = threading.Barrier(3)

    def process(message_id: int):
        barrier.wait()
        return db.process_referral_message_for_account(
            account_id=account_id,
            message_db_id=message_id,
            registry=registry,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(process, message_ids))

    assert all(result is not None for result in results)
    with db.connect() as conn:
        relationship = conn.execute(
            """
            SELECT meaningful_message_count, status, reward_ledger_id
            FROM referral_relationships
            WHERE invitee_platform_user_id=? AND app_id='test_product'
            """,
            (invitee["id"],),
        ).fetchone()
        rewards = conn.execute(
            """
            SELECT COUNT(*) AS n FROM entitlement_ledger
            WHERE platform_user_id=? AND app_id='test_product'
              AND source_type='referral_reward'
            """,
            (inviter_id,),
        ).fetchone()["n"]
    assert int(relationship["meaningful_message_count"]) == 3
    assert relationship["status"] == "rewarded"
    assert relationship["reward_ledger_id"] is not None
    assert int(rewards) == 1
