"""M4-2 lifecycle evidence、shadow scheduler 与 admin review queue。"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from datetime import datetime, timedelta

import app.db as db
import pytest
from app.db._backend import is_postgres
from app.domains.companion_world.lifecycle import (
    LifecycleEvidenceRef,
    LifecyclePolicy,
    inactivity_is_due,
    latest_crisis_freeze_until,
    value_misalignment_evidence,
)
from app.platform.companion_world_lifecycle import (
    CompanionWorldLifecycleService,
    build_lifecycle_policy,
)
from app.world_lifecycle.scheduler import WorldLifecycleScheduler

ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}
STAFF_HEADERS = {"Authorization": "Bearer test-staff"}
REVIEWER_HEADERS = {"Authorization": "Bearer test-reviewer"}


def _world(phone: str = "19965001001") -> tuple[str, dict]:
    owner_id = db.create_or_get_platform_user_by_phone(
        phone=phone, display_name="M4-2 owner"
    )["id"]
    world = db.get_or_create_home_universe(platform_user_id=owner_id)
    db.set_universe_onboarding_state(
        universe_id=world["id"], onboarding_state="confirmed"
    )
    return owner_id, db.get_universe(universe_id=world["id"])


def _resident(
    owner_id: str,
    world: dict,
    name: str,
    *,
    joined_at: str,
    origin: str = "preset",
) -> dict:
    template = db.create_character_template(source_type="official", name=name)
    runtime = db.create_resident_runtime_account(
        universe_id=world["id"],
        character_template_id=template["id"],
        display_name=name,
        joined_at=joined_at,
        origin=origin,
    )
    resident = runtime["resident"]
    db.create_ai_conversation(
        universe_id=world["id"],
        resident_id=resident["id"],
        owner_platform_user_id=owner_id,
        runtime_account_id=runtime["account"]["id"],
    )
    return {**resident, "runtime_account_id": runtime["account"]["id"]}


def _moderation_signal(
    account_id: str,
    *,
    key: str,
    at: str,
    category: str,
    explicit: bool = False,
    resident_id: str | None = None,
) -> dict:
    metadata = {"private_note": "must never leave the adapter"}
    risk_categories = []
    risk_level = "pass"
    status = "machine_passed"
    if explicit:
        metadata["lifecycle_observations"] = [
            {
                "category": category,
                "confirmed": True,
                "confidence": 0.93,
                "resident_id": resident_id,
                "raw_text": "must never be persisted",
            }
        ]
    else:
        risk_categories = [f"cat:{category}"]
        risk_level = "block" if category != "self_harm" else "review"
        status = "blocked" if risk_level == "block" else "needs_review"
    task = db.create_content_moderation_task(
        account_id=account_id,
        session_id=None,
        source_type="message",
        source_id=f"source-{key}",
        message_db_id=None,
        outbound_message_id=None,
        direction="inbound",
        content_kind="text",
        status=status,
        risk_level=risk_level,
        risk_categories=risk_categories,
        confidence=0.91,
        snapshot_text="private moderation snapshot",
        policy_version="moderation-test-v1",
        idempotency_key=f"m4-life:{key}",
        metadata=metadata,
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE content_moderation_tasks SET created_at=?, updated_at=? WHERE id=?",
            (at, at, task["id"]),
        )
    return task


def _insert_inbound(account_id: str, *, at: str, key: str) -> None:
    session = db.get_or_create_session(
        account_id=account_id,
        channel="native",
        sender_id="owner",
        sender_name=None,
        chat_id=None,
        session_key=db.APP_ACTIVE_SESSION_KEY,
        business_day=at[:10],
    )["session"]
    message_id = db.insert_message(
        account_id=account_id,
        session_id=int(session["id"]),
        message_id=key,
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="private owner message",
    )
    with db.connect() as conn:
        conn.execute("UPDATE messages SET created_at=? WHERE id=?", (at, message_id))


def test_lifecycle_domain_exact_boundaries():
    policy = LifecyclePolicy(version="test")
    now = datetime(2026, 7, 23, 10, 0, 0)
    assert inactivity_is_due(
        now=now,
        interaction_anchor_at=now - timedelta(days=60),
        policy=policy,
    )
    refs = tuple(
        LifecycleEvidenceRef(
            source_type="moderation_task",
            source_id=f"task-{index}",
            category="value_misalignment",
            observed_at=at,
        )
        for index, at in enumerate(
            (now - timedelta(days=30), now - timedelta(days=15), now)
        )
    )
    assert len(value_misalignment_evidence(now=now, observations=refs, policy=policy)) == 3
    assert latest_crisis_freeze_until(
        now=now,
        observations=(
            LifecycleEvidenceRef(
                source_type="moderation_task",
                source_id="crisis",
                category="cat:self_harm",
                observed_at=now - timedelta(days=30),
            ),
        ),
        policy=policy,
    ) is None


def test_inactivity_candidate_cooldown_and_recovery(fresh_db):
    owner_id, world = _world()
    target = _resident(
        owner_id, world, "inactive", joined_at="2026-05-01 10:00:00"
    )
    _resident(owner_id, world, "protector", joined_at="2026-07-22 10:00:00")
    service = CompanionWorldLifecycleService(policy=build_lifecycle_policy(fresh_db))
    now = datetime(2026, 7, 23, 10, 0, 0)

    created = service.evaluate_batch(now=now, after_resident_id=None, batch_size=50)
    event = db.list_resident_lifecycle_events_for_review(
        statuses=("cooling_down",)
    )[0]
    assert event["resident_id"] == target["id"]
    assert event["evidence_refs"][0]["source_type"] == "resident_joined"
    assert created["metrics"]["candidate_created"] == 1

    before = service.evaluate_batch(
        now=now + timedelta(days=6, hours=23),
        after_resident_id=None,
        batch_size=50,
    )
    assert before["metrics"]["cooldown_advanced"] == 0
    advanced = service.evaluate_batch(
        now=now + timedelta(days=7),
        after_resident_id=None,
        batch_size=50,
    )
    assert advanced["metrics"]["cooldown_advanced"] == 1
    assert db.get_resident_lifecycle_event(event_id=event["id"])["status"] == "review_pending"
    _insert_inbound(
        target["runtime_account_id"],
        at="2026-07-30 11:00:00",
        key="m4-recovery",
    )
    recovered = service.evaluate_batch(
        now=now + timedelta(days=7, hours=1),
        after_resident_id=None,
        batch_size=50,
    )
    assert recovered["metrics"]["recovery_cancelled"] == 1
    assert db.get_resident_lifecycle_event(event_id=event["id"])["status"] == "cancelled"


def test_value_mismatch_is_runtime_scoped_and_never_persists_plaintext(fresh_db):
    owner_id, world = _world("19965001002")
    target = _resident(owner_id, world, "target", joined_at="2026-07-01 10:00:00")
    other = _resident(owner_id, world, "other", joined_at="2026-07-01 10:00:00")
    for index, at in enumerate(
        ("2026-06-23 10:00:00", "2026-07-08 10:00:00", "2026-07-23 10:00:00")
    ):
        _moderation_signal(
            target["runtime_account_id"],
            key=f"value-{index}",
            at=at,
            category="value_misalignment",
            explicit=True,
            resident_id=target["id"],
        )
    _moderation_signal(
        other["runtime_account_id"],
        key="other-crisis",
        at="2026-07-22 10:00:00",
        category="self_harm",
    )
    service = CompanionWorldLifecycleService(policy=build_lifecycle_policy(fresh_db))
    service.evaluate_batch(
        now=datetime(2026, 7, 23, 10, 0, 0),
        after_resident_id=None,
        batch_size=50,
    )
    events = db.list_resident_lifecycle_events_for_review(statuses=("cooling_down",))
    assert len(events) == 1 and events[0]["resident_id"] == target["id"]
    encoded = str(events[0]["evidence_refs"])
    assert "private" not in encoded and "raw_text" not in encoded
    assert {item["category"] for item in events[0]["evidence_refs"]} == {
        "value_misalignment"
    }


def test_crisis_and_last_resident_block_ordinary_but_severe_abuse_queues(fresh_db):
    owner_id, world = _world("19965001003")
    resident = _resident(
        owner_id, world, "last", joined_at="2026-05-01 10:00:00"
    )
    service = CompanionWorldLifecycleService(policy=build_lifecycle_policy(fresh_db))
    now = datetime(2026, 7, 23, 10, 0, 0)
    blocked = service.evaluate_batch(now=now, after_resident_id=None, batch_size=50)
    assert blocked["metrics"]["last_resident_blocked"] == 1
    assert db.list_resident_lifecycle_events_for_review(
        statuses=("cooling_down", "review_pending")
    ) == []

    _moderation_signal(
        resident["runtime_account_id"],
        key="severe",
        at="2026-07-23 09:00:00",
        category="credible_threat",
    )
    queued = service.evaluate_batch(now=now, after_resident_id=None, batch_size=50)
    event = db.list_resident_lifecycle_events_for_review(
        statuses=("review_pending",)
    )[0]
    assert queued["metrics"]["candidate_created"] == 1
    assert event["event_type"] == "severe_abuse"
    assert bool(event["last_resident_exception_requested"]) is True

    crisis_owner, crisis_world = _world("19965001006")
    crisis_target = _resident(
        crisis_owner, crisis_world, "crisis", joined_at="2026-05-01 10:00:00"
    )
    _resident(
        crisis_owner, crisis_world, "crisis-other", joined_at="2026-07-01 10:00:00"
    )
    before_crisis = service.evaluate_batch(
        now=now, after_resident_id=None, batch_size=50
    )
    assert before_crisis["metrics"]["candidate_created"] == 1
    _moderation_signal(
        crisis_target["runtime_account_id"],
        key="crisis-freeze",
        at="2026-07-22 10:00:00",
        category="self_harm",
    )
    frozen = service.evaluate_batch(now=now, after_resident_id=None, batch_size=50)
    assert frozen["metrics"]["crisis_frozen"] == 1
    crisis_events = [
        item
        for item in db.list_resident_lifecycle_events_for_review(
            statuses=("cancelled",)
        )
        if item["resident_id"] == crisis_target["id"]
    ]
    assert crisis_events[0]["terminal_reason"] == "crisis_freeze_active"
    assert not any(
        item["resident_id"] == crisis_target["id"]
        for item in db.list_resident_lifecycle_events_for_review(
            statuses=("cooling_down", "review_pending")
        )
    )


def test_scheduler_flag_off_cursor_and_heartbeat(fresh_db):
    disabled = WorldLifecycleScheduler(
        enabled=False,
        interval_seconds=300,
        batch_size=1,
    )
    result = asyncio.run(disabled.run_once(now=datetime(2026, 7, 23, 10, 0, 0)))
    assert result["status"] == "disabled"
    assert db.list_resident_lifecycle_events_for_review(
        statuses=("cooling_down", "review_pending")
    ) == []

    owner_id, world = _world("19965001004")
    _resident(owner_id, world, "old-a", joined_at="2026-05-01 10:00:00")
    _resident(owner_id, world, "old-b", joined_at="2026-05-01 10:00:00")
    scheduler = WorldLifecycleScheduler(
        enabled=True,
        interval_seconds=300,
        batch_size=1,
    )
    first = asyncio.run(scheduler.run_once(now=datetime(2026, 7, 23, 10, 0, 0)))
    second = asyncio.run(scheduler.run_once(now=datetime(2026, 7, 23, 10, 0, 0)))
    assert first["next_after_resident_id"] is not None
    assert first["metrics"]["scanned"] == second["metrics"]["scanned"] == 1
    scheduler._heartbeat("ok")
    heartbeat = db.get_scheduler_heartbeat("world_lifecycle_scheduler")
    assert heartbeat["metadata"]["last_run_metrics"] == second["metrics"]
    assert "resident_id" not in heartbeat["metadata"]


def test_admin_lifecycle_queue_is_redacted_and_commit_stays_blocked(client, fresh_db):
    owner_id, world = _world("19965001005")
    resident = _resident(
        owner_id, world, "review", joined_at="2026-05-01 10:00:00"
    )
    _resident(owner_id, world, "other", joined_at="2026-07-01 10:00:00")
    service = CompanionWorldLifecycleService(policy=build_lifecycle_policy(fresh_db))
    service.evaluate_batch(
        now=datetime(2026, 7, 23, 10, 0, 0),
        after_resident_id=None,
        batch_size=50,
    )
    event = db.list_resident_lifecycle_events_for_review(statuses=("cooling_down",))[0]
    assert event["resident_id"] == resident["id"]

    assert client.get(
        "/admin/companion-world/lifecycle-events", headers=REVIEWER_HEADERS
    ).status_code == 403
    listed = client.get(
        "/admin/companion-world/lifecycle-events?status=cooling_down",
        headers=STAFF_HEADERS,
    )
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    listed_event = listed.json()["events"][0]
    assert listed_event["redacted"] is True
    assert "request_fingerprint" not in listed_event
    assert set(listed_event["evidence_refs"][0]) <= {
        "source_type",
        "source_id",
        "category",
        "observed_at",
        "confidence",
        "policy_version",
    }

    staff_approve = client.post(
        f"/admin/companion-world/lifecycle-events/{event['id']}/approve",
        headers=STAFF_HEADERS,
        json={"farewell_text": "再见", "reason": "review"},
    )
    assert staff_approve.status_code == 403
    admin_approve = client.post(
        f"/admin/companion-world/lifecycle-events/{event['id']}/approve",
        headers=ADMIN_HEADERS,
        json={"farewell_text": "再见", "reason": "review"},
    )
    assert admin_approve.status_code == 503
    assert admin_approve.json()["detail"]["code"] == "lifecycle_commit_disabled"
    rejected = client.post(
        f"/admin/companion-world/lifecycle-events/{event['id']}/reject",
        headers=STAFF_HEADERS,
        json={"reason": "insufficient_context"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["event"]["status"] == "rejected"


def test_pg_concurrent_candidate_creation_has_one_open_event(fresh_db):
    if not is_postgres():
        pytest.skip("lifecycle open-event 并发正确性以 PG 为准")
    owner_id, world = _world("19965001007")
    target = _resident(
        owner_id, world, "concurrent", joined_at="2026-05-01 10:00:00"
    )
    _resident(owner_id, world, "other", joined_at="2026-07-01 10:00:00")
    barrier = threading.Barrier(2)

    def _evaluate() -> int:
        local = CompanionWorldLifecycleService(policy=build_lifecycle_policy(fresh_db))
        barrier.wait(timeout=5)
        result = local.evaluate_batch(
            now=datetime(2026, 7, 23, 10, 0, 0),
            after_resident_id=None,
            batch_size=50,
        )
        return result["metrics"]["candidate_created"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        created_counts = list(executor.map(lambda _index: _evaluate(), range(2)))
    assert sum(created_counts) == 1
    events = db.list_resident_lifecycle_events_for_review(statuses=("cooling_down",))
    assert [item["resident_id"] for item in events].count(target["id"]) == 1
