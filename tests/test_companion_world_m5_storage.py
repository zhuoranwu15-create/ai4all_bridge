"""M5-1 schema、纯状态机、owner/visitor 隔离与 default-off 门禁。"""
from datetime import datetime, timedelta

import pytest

import app.db as db
from app.db._backend import IntegrityError
from app.db._core import _migration_0035_companion_world_visit_human_chat
from app.products.zhaoxi.domain.companion_world.human_chat import (
    human_conversation_transition_allowed,
    human_message_fingerprint,
    normalize_human_message_body,
)
from app.products.zhaoxi.domain.companion_world.visits import (
    VisitPolicy,
    active_expires_at,
    invite_expires_at,
    is_due,
    pending_expires_at,
    visit_transition_allowed,
)

NOW = "2026-07-23 12:00:00"


def _user(phone: str) -> str:
    return db.create_or_get_platform_user_by_phone(phone=phone)["id"]


def _world(owner_id: str) -> dict:
    world = db.get_or_create_home_universe(platform_user_id=owner_id)
    db.set_universe_onboarding_state(
        universe_id=world["id"], onboarding_state="confirmed"
    )
    return db.get_universe(universe_id=world["id"])


def _invite(owner_id: str, world: dict, suffix: str = "1") -> dict:
    return db.insert_universe_invite(
        universe_id=world["id"],
        owner_platform_user_id=owner_id,
        code_hash=f"hash-{suffix}",
        code_prefix=f"code{suffix}",
        expires_at="2026-07-24 12:00:00",
        created_at=NOW,
    )


def _pending(owner_id: str, visitor_id: str, world: dict, suffix: str = "1") -> dict:
    invite = _invite(owner_id, world, suffix)
    return db.insert_pending_universe_visit(
        invite_id=invite["id"],
        universe_id=world["id"],
        owner_platform_user_id=owner_id,
        visitor_platform_user_id=visitor_id,
        pending_expires_at="2026-07-30 12:00:00",
        created_at=NOW,
    )


def test_m0035_tables_idempotency_and_flags_default_off(fresh_db):
    for table in (
        "universe_visit_slots",
        "universe_invites",
        "universe_visits",
        "human_conversations",
        "human_messages",
        "platform_user_blocks",
        "human_chat_reports",
    ):
        with db.connect() as conn:
            conn.execute(f"SELECT 1 FROM {table} WHERE 1 = 0").fetchall()
    with db.connect() as conn:
        _migration_0035_companion_world_visit_human_chat(conn)
        _migration_0035_companion_world_visit_human_chat(conn)
        version = conn.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()["version"]
    assert int(version) == 46
    assert fresh_db.companion_world_visits_enabled is False
    assert fresh_db.companion_world_human_chat_enabled is False


def test_visit_domain_exact_expiry_and_one_way_state_machine():
    policy = VisitPolicy()
    created = datetime(2026, 7, 23, 12, 0, 0)
    assert invite_expires_at(created_at=created, policy=policy) == created + timedelta(
        hours=24
    )
    assert pending_expires_at(redeemed_at=created, policy=policy) == created + timedelta(
        days=7
    )
    assert active_expires_at(accepted_at=created, policy=policy) == created + timedelta(
        days=30
    )
    assert is_due(now=created, expires_at=created) is True
    assert visit_transition_allowed("pending", "active") is True
    assert visit_transition_allowed("active", "pending") is False
    assert visit_transition_allowed("expired", "active") is False


def test_human_chat_domain_normalization_and_read_only_is_terminal():
    assert normalize_human_message_body("  hello  ") == "hello"
    with pytest.raises(ValueError):
        normalize_human_message_body("  ")
    with pytest.raises(ValueError):
        normalize_human_message_body("x" * 4001)
    assert human_conversation_transition_allowed("active", "read_only") is True
    assert human_conversation_transition_allowed("read_only", "active") is False
    assert human_message_fingerprint(
        client_message_id="c1", body_text="hello"
    ) == human_message_fingerprint(client_message_id="c1", body_text="hello")


def test_owner_slots_invite_hash_and_owner_isolation(fresh_db):
    owner = _user("19965001001")
    outsider = _user("19965001002")
    world = _world(owner)
    invite = _invite(owner, world)

    slots = db.ensure_universe_visit_slots(
        universe_id=world["id"], owner_platform_user_id=owner
    )
    assert [slot["slot_no"] for slot in slots] == [1, 2, 3]
    assert slots[0]["occupant_type"] == "invite"
    assert slots[0]["occupant_id"] == invite["id"]
    assert db.get_universe_invite_for_owner(
        invite_id=invite["id"], owner_platform_user_id=owner
    )["id"] == invite["id"]
    assert db.get_universe_invite_for_owner(
        invite_id=invite["id"], owner_platform_user_id=outsider
    ) is None
    assert db.get_universe_invite_by_code_hash(code_hash="hash-1")["id"] == invite["id"]
    assert db.list_universe_invites_for_owner(owner_platform_user_id=outsider) == []

    with pytest.raises(IntegrityError):
        db.insert_universe_invite(
            universe_id=world["id"],
            owner_platform_user_id=owner,
            code_hash="hash-1",
            code_prefix="other",
            expires_at="2026-07-24 12:00:00",
            created_at=NOW,
        )


def test_invite_slot_transfers_to_pending_visit_and_participant_scope(fresh_db):
    owner = _user("19965002001")
    visitor = _user("19965002002")
    outsider = _user("19965002003")
    world = _world(owner)
    visit = _pending(owner, visitor, world)

    assert db.count_open_universe_visits_for_visitor(
        visitor_platform_user_id=visitor
    ) == 1
    assert db.get_universe_visit_for_participant(
        visit_id=visit["id"], platform_user_id=owner
    )["id"] == visit["id"]
    assert db.get_universe_visit_for_participant(
        visit_id=visit["id"], platform_user_id=visitor
    )["id"] == visit["id"]
    assert db.get_universe_visit_for_participant(
        visit_id=visit["id"], platform_user_id=outsider
    ) is None
    with db.connect() as conn:
        slot = conn.execute(
            "SELECT occupant_type, occupant_id FROM universe_visit_slots "
            "WHERE universe_id = ? AND slot_no = 1",
            (world["id"],),
        ).fetchone()
    assert dict(slot) == {"occupant_type": "visit", "occupant_id": visit["id"]}

    second_invite = _invite(owner, world, "2")
    with pytest.raises(IntegrityError):
        db.insert_pending_universe_visit(
            invite_id=second_invite["id"],
            universe_id=world["id"],
            owner_platform_user_id=owner,
            visitor_platform_user_id=visitor,
            pending_expires_at="2026-07-30 12:00:00",
            created_at=NOW,
        )


def test_human_conversation_participant_scope_and_bidirectional_block(fresh_db):
    owner = _user("19965003001")
    visitor = _user("19965003002")
    outsider = _user("19965003003")
    world = _world(owner)
    visit = _pending(owner, visitor, world)
    with db.connect() as conn:
        conn.execute(
            "UPDATE universe_visits SET status = 'active', accepted_at = ?, "
            "expires_at = ? WHERE id = ?",
            (NOW, "2026-08-22 12:00:00", visit["id"]),
        )
    conversation = db.create_human_conversation_for_visit(
        visit_id=visit["id"],
        owner_platform_user_id=owner,
        visitor_platform_user_id=visitor,
        created_at=NOW,
    )
    replay = db.create_human_conversation_for_visit(
        visit_id=visit["id"],
        owner_platform_user_id=owner,
        visitor_platform_user_id=visitor,
        created_at=NOW,
    )
    assert replay["id"] == conversation["id"]
    assert db.get_human_conversation_for_participant(
        conversation_id=conversation["id"], platform_user_id=owner
    )["id"] == conversation["id"]
    assert db.get_human_conversation_for_participant(
        conversation_id=conversation["id"], platform_user_id=outsider
    ) is None
    assert db.list_human_conversations_for_participant(
        platform_user_id=visitor
    )[0]["id"] == conversation["id"]

    assert db.insert_platform_user_block(
        blocker_platform_user_id=visitor,
        blocked_platform_user_id=owner,
        created_at=NOW,
    ) is True
    assert db.insert_platform_user_block(
        blocker_platform_user_id=visitor,
        blocked_platform_user_id=owner,
        created_at=NOW,
    ) is False
    assert db.has_platform_user_block(
        first_platform_user_id=owner, second_platform_user_id=visitor
    ) is True
