"""M4-1 lifecycle/mailbox schema、存储幂等、owner 隔离与终态边界。"""
import pytest

import app.db as db
from app.db._backend import IntegrityError
from app.products.mingchan.domain.companion_world.lifecycle import (
    lifecycle_event_fingerprint,
    lifecycle_transition_allowed,
)
from app.products.mingchan.domain.companion_world.mailbox import (
    letter_delivery_fingerprint,
    letter_is_open,
    letter_transition_allowed,
)


def _owner(phone: str = "19964001001") -> str:
    return db.create_or_get_platform_user_by_phone(
        phone=phone, display_name="M4 测试用户"
    )["id"]


def _runtime_account(name: str = "M4 居民") -> str:
    from app.db._core import _new_account_id

    account_id = _new_account_id()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO accounts(id, channel, display_name, app_id, updated_at) "
            "VALUES (?, 'native', ?, 'zhaoxi', "
            "to_char((now() AT TIME ZONE 'Asia/Shanghai'), "
            "'YYYY-MM-DD HH24:MI:SS'))",
            (account_id, name),
        )
    return account_id


def _confirmed_world(owner_id: str) -> dict:
    world = db.get_or_create_home_universe(platform_user_id=owner_id)
    db.set_universe_onboarding_state(
        universe_id=world["id"], onboarding_state="confirmed"
    )
    return db.get_universe(universe_id=world["id"])


def _template(name: str, *, version: str = "v1") -> dict:
    return db.create_character_template(
        source_type="official", name=name, persona_version=version
    )


def _resident(
    owner_id: str,
    *,
    origin: str = "preset",
    name: str = "M4 居民",
) -> tuple[dict, dict, dict]:
    world = _confirmed_world(owner_id)
    template = _template(name)
    account_id = _runtime_account(name)
    resident = db.create_resident(
        universe_id=world["id"],
        character_template_id=template["id"],
        template_version=template["persona_version"],
        origin=origin,
        status="active",
        runtime_account_id=account_id,
    )
    db.create_ai_conversation(
        universe_id=world["id"],
        resident_id=resident["id"],
        owner_platform_user_id=owner_id,
        runtime_account_id=account_id,
    )
    return world, template, resident


def _lifecycle_event(owner_id: str, world: dict, resident: dict):
    refs = ({"message_id": "msg_1", "category": "value_misalignment"},)
    fingerprint = lifecycle_event_fingerprint(
        resident_id=resident["id"],
        event_type="value_misalignment",
        policy_version="life-v1",
        evidence_window_start="2026-06-01 10:00:00",
        evidence_window_end="2026-07-01 10:00:00",
        evidence_count=3,
        evidence_refs=refs,
        cooldown_until="2026-07-08 10:00:00",
        crisis_freeze_until=None,
        last_resident_exception_requested=False,
    )
    return db.create_resident_lifecycle_event(
        owner_platform_user_id=owner_id,
        universe_id=world["id"],
        resident_id=resident["id"],
        event_type="value_misalignment",
        status="cooling_down",
        policy_version="life-v1",
        evidence_window_start="2026-06-01 10:00:00",
        evidence_window_end="2026-07-01 10:00:00",
        evidence_count=3,
        evidence_refs=refs,
        cooldown_until="2026-07-08 10:00:00",
        crisis_freeze_until=None,
        last_resident_exception_requested=False,
        idempotency_key=f"lifecycle:{resident['id']}:value:20260701",
        request_fingerprint=fingerprint,
        actor_type="scheduler",
        actor_id="world-lifecycle",
        created_at="2026-07-01 10:00:00",
    )


def _catalog(*, key: str = "stranger-a", name: str = "远方来客", version: str = "v1"):
    template = _template(name, version=version)
    catalog, created = db.create_character_letter_catalog_entry(
        character_key=key,
        character_template_id=template["id"],
        template_version=version,
        letter_body="你好，我在寻找一处安静的精神栖息地。",
        policy_version="mail-v1",
        priority=10,
        created_by="admin-test",
    )
    assert created is True
    return template, catalog


def _letter(owner_id: str, world: dict, catalog: dict, *, key: str = "letter-key"):
    snapshot = {"active_count": 3, "active_threshold": 8}
    fingerprint = letter_delivery_fingerprint(
        universe_id=world["id"],
        catalog_id=catalog["id"],
        character_key=catalog["character_key"],
        template_version=catalog["template_version"],
        delivered_at="2026-07-01 10:00:00",
        expires_at="2026-07-31 10:00:00",
        policy_version="mail-v1",
        eligibility_snapshot=snapshot,
    )
    return db.insert_character_letter(
        owner_platform_user_id=owner_id,
        universe_id=world["id"],
        catalog_id=catalog["id"],
        idempotency_key=key,
        request_fingerprint=fingerprint,
        eligibility_snapshot=snapshot,
        policy_version="mail-v1",
        delivered_at="2026-07-01 10:00:00",
        expires_at="2026-07-31 10:00:00",
    )


def test_m0034_tables_columns_and_config_defaults(fresh_db):
    for table in (
        "resident_lifecycle_events",
        "resident_lifecycle_event_actions",
        "character_letter_catalog",
        "character_letters",
    ):
        with db.connect() as conn:
            conn.execute(f"SELECT 1 FROM {table} WHERE 1 = 0").fetchall()
    with db.connect() as conn:
        conn.execute(
            "SELECT post_type, departure_event_id FROM universe_posts WHERE 1 = 0"
        ).fetchall()
    assert fresh_db.companion_world_lifecycle_evaluation_enabled is False
    assert fresh_db.companion_world_lifecycle_commit_enabled is False
    assert fresh_db.companion_world_mailbox_enabled is False
    assert fresh_db.companion_world_lifecycle_inactivity_days == 60
    assert fresh_db.companion_world_mailbox_letter_ttl_days == 30


def test_lifecycle_event_idempotency_actions_and_transition(fresh_db):
    owner_id = _owner()
    world, _template_row, resident = _resident(owner_id)

    event, created = _lifecycle_event(owner_id, world, resident)
    replay, replay_created = _lifecycle_event(owner_id, world, resident)

    assert created is True
    assert replay_created is False
    assert replay["id"] == event["id"]
    assert event["evidence_refs"][0]["message_id"] == "msg_1"
    assert [item["action"] for item in db.list_resident_lifecycle_event_actions(event_id=event["id"])] == [
        "candidate_created"
    ]

    other_owner = _owner("19964001003")
    other_world, _other_template, other_resident = _resident(
        other_owner, name="另一世界居民"
    )
    with pytest.raises(ValueError, match="idempotency conflict"):
        db.create_resident_lifecycle_event(
            owner_platform_user_id=other_owner,
            universe_id=other_world["id"],
            resident_id=other_resident["id"],
            event_type="value_misalignment",
            status="cooling_down",
            policy_version="life-v1",
            evidence_window_start="2026-06-01 10:00:00",
            evidence_window_end="2026-07-01 10:00:00",
            evidence_count=3,
            evidence_refs=event["evidence_refs"],
            cooldown_until="2026-07-08 10:00:00",
            crisis_freeze_until=None,
            last_resident_exception_requested=False,
            idempotency_key=event["idempotency_key"],
            request_fingerprint=event["request_fingerprint"],
            actor_type="scheduler",
            actor_id="world-lifecycle",
            created_at="2026-07-01 10:00:00",
        )

    review = db.transition_resident_lifecycle_event(
        event_id=event["id"],
        expected_status="cooling_down",
        new_status="review_pending",
        action="cooldown_revalidated",
        actor_type="scheduler",
        actor_id="world-lifecycle",
        now="2026-07-08 10:00:00",
    )
    assert review["status"] == "review_pending"
    rejected = db.transition_resident_lifecycle_event(
        event_id=event["id"],
        expected_status="review_pending",
        new_status="rejected",
        action="admin_rejected",
        actor_type="admin",
        actor_id="admin-test",
        now="2026-07-08 11:00:00",
        terminal_reason="insufficient_context",
    )
    assert rejected["status"] == "rejected"
    assert [item["action"] for item in db.list_resident_lifecycle_event_actions(event_id=event["id"])] == [
        "candidate_created",
        "cooldown_revalidated",
        "admin_rejected",
    ]


def test_lifecycle_storage_rejects_owner_legacy_and_committed_shortcut(fresh_db):
    owner_id = _owner()
    world, _template_row, resident = _resident(owner_id)
    with pytest.raises(ValueError, match="ownership mismatch"):
        _lifecycle_event("pu_other", world, resident)

    legacy_owner = _owner("19964001002")
    legacy_world, _legacy_template, legacy = _resident(
        legacy_owner, origin="legacy", name="旧居民"
    )
    with pytest.raises(ValueError, match="legacy resident"):
        _lifecycle_event(legacy_owner, legacy_world, legacy)

    event, _created = _lifecycle_event(owner_id, world, resident)
    with pytest.raises(ValueError, match="atomic offline"):
        db.transition_resident_lifecycle_event(
            event_id=event["id"],
            expected_status="cooling_down",
            new_status="committed",
            action="invalid_commit",
            actor_type="admin",
            actor_id="admin-test",
            now="2026-07-08 10:00:00",
        )


def test_lifecycle_only_one_open_event_per_resident(fresh_db):
    owner_id = _owner()
    world, _template_row, resident = _resident(owner_id)
    _lifecycle_event(owner_id, world, resident)
    with pytest.raises(ValueError, match="already open"):
        db.create_resident_lifecycle_event(
            owner_platform_user_id=owner_id,
            universe_id=world["id"],
            resident_id=resident["id"],
            event_type="inactivity",
            status="cooling_down",
            policy_version="life-v1",
            evidence_window_start="2026-05-01 10:00:00",
            evidence_window_end="2026-07-01 10:00:00",
            evidence_count=1,
            evidence_refs=({"kind": "last_inbound"},),
            cooldown_until="2026-07-08 10:00:00",
            crisis_freeze_until=None,
            last_resident_exception_requested=False,
            idempotency_key=f"lifecycle:{resident['id']}:inactive:20260701",
            request_fingerprint="different-fingerprint",
            actor_type="scheduler",
            actor_id="world-lifecycle",
            created_at="2026-07-01 10:00:00",
        )


def test_catalog_is_versioned_and_retire_is_idempotent(fresh_db):
    _template_v1, catalog_v1 = _catalog()
    replay, created = db.create_character_letter_catalog_entry(
        character_key="stranger-a",
        character_template_id=catalog_v1["character_template_id"],
        template_version="v1",
        letter_body=catalog_v1["letter_body"],
        policy_version="mail-v1",
        priority=10,
        created_by="admin-test",
    )
    assert created is False and replay["id"] == catalog_v1["id"]
    retired = db.retire_character_letter_catalog_entry(
        catalog_id=catalog_v1["id"],
        retired_by="admin-test",
        retired_at="2026-07-02 10:00:00",
    )
    assert retired["status"] == "retired"
    again = db.retire_character_letter_catalog_entry(
        catalog_id=catalog_v1["id"],
        retired_by="admin-test",
        retired_at="2026-07-02 10:00:00",
    )
    assert again["status"] == "retired"

    _template_v2, catalog_v2 = _catalog(version="v2")
    assert catalog_v2["character_key"] == catalog_v1["character_key"]
    assert [item["id"] for item in db.list_character_letter_catalog(statuses=("active",))] == [
        catalog_v2["id"]
    ]


def test_letter_owner_isolation_idempotency_and_open_cap(fresh_db):
    owner_id = _owner()
    world = _confirmed_world(owner_id)
    _template_row, catalog = _catalog()
    letter, created = _letter(owner_id, world, catalog)
    replay, replay_created = _letter(owner_id, world, catalog)

    assert created is True and replay_created is False
    assert replay["id"] == letter["id"]
    assert db.get_character_letter_for_owner(
        letter_id=letter["id"], owner_platform_user_id=owner_id
    )["character_name"] == "远方来客"
    assert db.get_character_letter_for_owner(
        letter_id=letter["id"], owner_platform_user_id="pu_other"
    ) is None
    assert db.count_unread_character_letters(
        owner_platform_user_id=owner_id, now="2026-07-02 10:00:00"
    ) == 1

    other_owner = _owner("19964001004")
    other_world = _confirmed_world(other_owner)
    with pytest.raises(ValueError, match="idempotency conflict"):
        db.insert_character_letter(
            owner_platform_user_id=other_owner,
            universe_id=other_world["id"],
            catalog_id=catalog["id"],
            idempotency_key=letter["idempotency_key"],
            request_fingerprint=letter["request_fingerprint"],
            eligibility_snapshot={"active_count": 0},
            policy_version="mail-v1",
            delivered_at="2026-07-01 10:00:00",
            expires_at="2026-07-31 10:00:00",
        )

    _template_b, catalog_b = _catalog(key="stranger-b", name="第二位来客")
    with pytest.raises(IntegrityError):
        _letter(owner_id, world, catalog_b, key="letter-key-b")


def test_letter_transitions_are_owner_scoped_and_accept_is_reserved(fresh_db):
    owner_id = _owner()
    world = _confirmed_world(owner_id)
    _template_row, catalog = _catalog()
    letter, _created = _letter(owner_id, world, catalog)

    read = db.transition_open_character_letter(
        letter_id=letter["id"],
        owner_platform_user_id=owner_id,
        new_status="read",
        now="2026-07-02 10:00:00",
    )
    assert read["status"] == "read" and read["read_at"] is not None
    deferred = db.transition_open_character_letter(
        letter_id=letter["id"],
        owner_platform_user_id=owner_id,
        new_status="deferred",
        now="2026-07-03 10:00:00",
    )
    assert deferred["status"] == "deferred"
    assert db.transition_open_character_letter(
        letter_id=letter["id"],
        owner_platform_user_id="pu_other",
        new_status="declined",
        now="2026-07-04 10:00:00",
        terminal_reason="owner_declined",
    ) is None
    with pytest.raises(ValueError, match="atomic resident"):
        db.transition_open_character_letter(
            letter_id=letter["id"],
            owner_platform_user_id=owner_id,
            new_status="accepted",
            now="2026-07-04 10:00:00",
        )
    declined = db.transition_open_character_letter(
        letter_id=letter["id"],
        owner_platform_user_id=owner_id,
        new_status="declined",
        now="2026-07-04 10:00:00",
        terminal_reason="owner_declined",
    )
    assert declined["status"] == "declined"
    assert db.count_unread_character_letters(
        owner_platform_user_id=owner_id, now="2026-07-05 10:00:00"
    ) == 0


def test_pure_domain_state_helpers():
    assert lifecycle_transition_allowed("cooling_down", "review_pending") is True
    assert lifecycle_transition_allowed("cooling_down", "committed") is False
    assert letter_is_open("deferred") is True
    assert letter_transition_allowed("unread", "accepted") is True
    assert letter_transition_allowed("expired", "accepted") is False
