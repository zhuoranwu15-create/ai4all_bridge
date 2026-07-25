"""legacy backfill：0/N/10 binding、幂等、无历史/钱包/人设副作用。"""
import pytest

import app.db as db
from app.bootstrap.product_registry import build_test_product_registry
from app.db._core import _new_id
from scripts.backfill_companion_world import run_backfill


@pytest.fixture(autouse=True)
def _restore_active_owner_index(fresh_db):
    """测试 legacy 重复绑定后恢复产品唯一索引，避免污染同进程后续用例。"""
    yield
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM account_owner_bindings WHERE binding_method='legacy_test'"
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_owner_binding_active_user_app
            ON account_owner_bindings(platform_user_id, app_id)
            WHERE status='active'
            """
        )


def _user(phone: str) -> str:
    return db.create_or_get_platform_user_by_phone(phone=phone, display_name="存量用户")["id"]


def _legacy_accounts(user_id: str, count: int) -> list[str]:
    if count <= 0:
        return []
    accounts = [
        db.create_ai4all_account_for_user(
            platform_user_id=user_id, display_name="存量角色0"
        )["account"]["id"]
    ]
    if count == 1:
        return accounts

    # backfill 面向收敛前同一真人在朝夕拥有多账号的历史形态；测试库已启用新的
    # (platform_user_id, app_id) 唯一约束，因此仅在该 fixture 中撤掉索引并直写 legacy 行。
    with db.connect() as conn:
        conn.execute("DROP INDEX IF EXISTS ux_owner_binding_active_user_app")
        for index in range(1, count):
            account_id = _new_id("aid_legacy")
            conn.execute(
                """
                INSERT INTO accounts(id, channel, display_name, app_id)
                VALUES (?, 'openclaw-weixin', ?, 'zhaoxi')
                """,
                (account_id, f"存量角色{index}"),
            )
            conn.execute(
                "INSERT INTO profiles(account_id, display_name) VALUES (?, ?)",
                (account_id, f"存量角色{index}"),
            )
            conn.execute(
                """
                INSERT INTO account_owner_bindings(
                    platform_user_id, account_id, binding_method, status, app_id
                ) VALUES (?, ?, 'legacy_test', 'active', 'zhaoxi')
                """,
                (user_id, account_id),
            )
            accounts.append(account_id)
    return accounts


def _table_counts() -> dict:
    with db.connect() as conn:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"])
            for table in (
                "accounts",
                "profiles",
                "sessions",
                "messages",
                "entitlement_wallets",
                "entitlement_ledger",
                "subscriptions",
                "account_profile_files",
            )
        }


def test_backfill_dry_run_is_read_only(fresh_db):
    user_id = _user("19940001001")
    _legacy_accounts(user_id, 1)
    report = run_backfill(dry_run=True)
    assert report.scanned_users == 1 and report.mapped_bindings == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM universes").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM universe_residents").fetchone()["c"] == 0


def test_zero_binding_user_gets_preparing_world_without_presets(fresh_db):
    user_id = _user("19940001002")
    report = run_backfill(dry_run=False)
    assert report.zero_binding_users == 1 and not report.errors
    world = db.get_universe(owner_platform_user_id=user_id)
    assert world["onboarding_state"] == "preparing"
    assert db.list_residents(universe_id=world["id"], statuses=("active", "candidate")) == []


def test_all_bindings_map_to_legacy_idempotently_without_runtime_side_effects(fresh_db):
    user_id = _user("19940001003")
    accounts = _legacy_accounts(user_id, 3)
    session = db.get_or_create_session(
        account_id=accounts[1],
        channel="openclaw-weixin",
        sender_id="u",
        sender_name=None,
        chat_id="u",
        session_key="legacy-history",
    )["session"]
    db.insert_message(
        account_id=accounts[1],
        session_id=int(session["id"]),
        message_id="legacy-message",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="历史原文保持不变",
    )
    before = _table_counts()

    first = run_backfill(dry_run=False)
    second = run_backfill(dry_run=False)
    assert first.mapped_bindings == second.mapped_bindings == 3
    assert not first.errors and not second.errors
    after = _table_counts()
    assert after == before

    world = db.get_universe(owner_platform_user_id=user_id)
    residents = db.list_residents(universe_id=world["id"], statuses=("active",))
    assert len(residents) == 3
    assert {row["runtime_account_id"] for row in residents} == set(accounts)
    assert {row["origin"] for row in residents} == {"legacy"}
    assert world["legacy_primary_account_id"] == accounts[0]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM ai_conversations").fetchone()["c"] == 3
        assert conn.execute(
            "SELECT content FROM messages WHERE message_id='legacy-message'"
        ).fetchone()["content"] == "历史原文保持不变"


def test_ten_bindings_all_preserved_and_reported_full(fresh_db):
    user_id = _user("19940001004")
    accounts = _legacy_accounts(user_id, 10)
    report = run_backfill(dry_run=False)
    assert report.full_capacity_users == 1 and report.mapped_bindings == 10
    world = db.get_universe(owner_platform_user_id=user_id)
    residents = db.list_residents(universe_id=world["id"], statuses=("active",))
    assert len(residents) == 10
    assert {row["runtime_account_id"] for row in residents} == set(accounts)


def test_backfill_excludes_other_product_bindings_from_counts_and_mapping(fresh_db):
    registry = build_test_product_registry()
    user_id = _user("19940001005")
    zhaoxi_id = _legacy_accounts(user_id, 1)[0]
    db.ensure_product_membership(
        platform_user_id=user_id, app_id="test_product", registry=registry
    )
    other_id = db.create_ai4all_account_for_user(
        platform_user_id=user_id,
        display_name="其他产品角色",
        app_id="test_product",
        registry=registry,
    )["account"]["id"]

    preview = run_backfill(dry_run=True)
    assert preview.mapped_bindings == 1
    assert db.list_active_account_ids_for_user(platform_user_id=user_id) == [zhaoxi_id]

    applied = run_backfill(dry_run=False)
    assert applied.mapped_bindings == 1 and not applied.errors
    world = db.get_universe(owner_platform_user_id=user_id)
    residents = db.list_residents(universe_id=world["id"], statuses=("active",))
    assert {row["runtime_account_id"] for row in residents} == {zhaoxi_id}
    assert other_id not in {row["runtime_account_id"] for row in residents}
