"""legacy backfill：0/N/10 binding、幂等、无历史/钱包/人设副作用。"""
import app.db as db
from scripts.backfill_companion_world import run_backfill


def _user(phone: str) -> str:
    return db.create_or_get_platform_user_by_phone(phone=phone, display_name="存量用户")["id"]


def _legacy_accounts(user_id: str, count: int) -> list[str]:
    return [
        db.create_ai4all_account_for_user(
            platform_user_id=user_id,
            display_name=f"存量角色{index}",
            app_id=f"legacy-app-{index}",
        )["account"]["id"]
        for index in range(count)
    ]


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
