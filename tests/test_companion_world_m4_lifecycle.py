"""M4 lifecycle evidence、review 与 offline 原子事务。"""
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
    lifecycle_event_fingerprint,
    value_misalignment_evidence,
)
from app.platform.companion_world_lifecycle import (
    CompanionWorldLifecycleService,
    LifecycleCommitError,
    approve_lifecycle_event,
    build_lifecycle_policy,
)
from app.platform.companion_world_repository import SqlCompanionWorldRepository
from app.world_lifecycle.scheduler import WorldLifecycleScheduler

ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}
STAFF_HEADERS = {"Authorization": "Bearer test-staff"}
REVIEWER_HEADERS = {"Authorization": "Bearer test-reviewer"}
NOW = datetime(2026, 7, 23, 10, 0, 0)


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


def _review_event(
    owner_id: str,
    world: dict,
    resident: dict,
    *,
    event_type: str = "inactivity",
    last_resident_exception_requested: bool = False,
) -> dict:
    policy_version = build_lifecycle_policy().version
    cooldown_until = None if event_type == "severe_abuse" else "2026-07-23 09:00:00"
    refs = (
        {
            "source_type": "resident_joined",
            "source_id": resident["id"],
            "category": event_type,
            "observed_at": "2026-05-01 10:00:00",
            "policy_version": policy_version,
        },
    )
    fingerprint = lifecycle_event_fingerprint(
        resident_id=resident["id"],
        event_type=event_type,
        policy_version=policy_version,
        evidence_window_start="2026-05-01 10:00:00",
        evidence_window_end="2026-07-23 10:00:00",
        evidence_count=1,
        evidence_refs=refs,
        cooldown_until=cooldown_until,
        crisis_freeze_until=None,
        last_resident_exception_requested=last_resident_exception_requested,
    )
    event, created = db.create_resident_lifecycle_event(
        owner_platform_user_id=owner_id,
        universe_id=world["id"],
        resident_id=resident["id"],
        event_type=event_type,
        status="review_pending",
        policy_version=policy_version,
        evidence_window_start="2026-05-01 10:00:00",
        evidence_window_end="2026-07-23 10:00:00",
        evidence_count=1,
        evidence_refs=refs,
        cooldown_until=cooldown_until,
        crisis_freeze_until=None,
        last_resident_exception_requested=last_resident_exception_requested,
        idempotency_key=f"m4-review:{resident['id']}:{event_type}",
        request_fingerprint=fingerprint,
        actor_type="scheduler",
        actor_id="test",
        created_at="2026-07-23 09:00:00",
    )
    assert created is True
    return event


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


def test_admin_approve_atomically_offlines_and_correction_hides_farewell(
    client, fresh_db, monkeypatch
):
    owner_id, world = _world("19965001008")
    target = _resident(
        owner_id, world, "departing", joined_at="2026-05-01 10:00:00"
    )
    _resident(owner_id, world, "protector", joined_at="2026-07-01 10:00:00")
    event = _review_event(owner_id, world, target)
    fresh_db.companion_world_lifecycle_commit_enabled = True
    monkeypatch.setattr(
        "app.routers.admin_companion_world.settings.companion_world_lifecycle_commit_enabled",
        True,
    )
    monkeypatch.setattr(
        "app.routers.admin_companion_world.beijing_naive_now", lambda: NOW
    )

    approved = client.post(
        f"/admin/companion-world/lifecycle-events/{event['id']}/approve",
        headers=ADMIN_HEADERS,
        json={"farewell_text": "愿你在自己的世界里，一直被温柔照亮。", "reason": "approved"},
    )
    replay = client.post(
        f"/admin/companion-world/lifecycle-events/{event['id']}/approve",
        headers=ADMIN_HEADERS,
        json={"farewell_text": "重试不会再发一条", "reason": "retry"},
    )
    assert approved.status_code == replay.status_code == 200
    assert approved.json()["event"]["status"] == "committed"
    assert approved.json()["replayed"] is False
    assert replay.json()["replayed"] is True
    assert replay.json()["farewell_post"]["post_id"] == approved.json()[
        "farewell_post"
    ]["post_id"]
    assert approved.headers["cache-control"] == "no-store"

    with db.connect() as conn:
        resident = conn.execute(
            "SELECT * FROM universe_residents WHERE id=?", (target["id"],)
        ).fetchone()
        target_conversation = conn.execute(
            "SELECT * FROM ai_conversations WHERE resident_id=?", (target["id"],)
        ).fetchone()
        posts = conn.execute(
            "SELECT * FROM universe_posts WHERE departure_event_id=?", (event["id"],)
        ).fetchall()
        outboxes = conn.execute(
            "SELECT * FROM companion_world_outbox WHERE idempotency_key=?",
            (f"departure-farewell:v1:{event['id']}",),
        ).fetchall()
    assert resident["status"] == "offline"
    assert resident["departure_event_id"] == event["id"]
    assert target_conversation["state"] == "read_only"
    assert len(posts) == len(outboxes) == 1
    assert posts[0]["post_type"] == "farewell"
    assert posts[0]["source_type"] == "lifecycle_farewell"

    corrected = client.post(
        f"/admin/companion-world/lifecycle-events/{event['id']}/correct",
        headers=ADMIN_HEADERS,
        json={"reason": "farewell wording correction", "hide_farewell": True},
    )
    assert corrected.status_code == 200
    assert corrected.json()["event"]["correction_status"] == "farewell_hidden"
    with db.connect() as conn:
        assert conn.execute(
            "SELECT status FROM universe_posts WHERE departure_event_id=?",
            (event["id"],),
        ).fetchone()["status"] == "deleted"
        assert conn.execute(
            "SELECT status FROM universe_residents WHERE id=?", (target["id"],)
        ).fetchone()["status"] == "offline"
        assert conn.execute(
            "SELECT state FROM ai_conversations WHERE resident_id=?", (target["id"],)
        ).fetchone()["state"] == "read_only"


def test_approve_revalidates_crisis_legacy_last_resident_and_evidence(
    client, fresh_db, monkeypatch
):
    fresh_db.companion_world_lifecycle_commit_enabled = True
    monkeypatch.setattr(
        "app.routers.admin_companion_world.settings.companion_world_lifecycle_commit_enabled",
        True,
    )
    monkeypatch.setattr(
        "app.routers.admin_companion_world.beijing_naive_now", lambda: NOW
    )
    owner_id, world = _world("19965001009")
    target = _resident(owner_id, world, "ordinary", joined_at="2026-05-01 10:00:00")
    _resident(owner_id, world, "other", joined_at="2026-07-01 10:00:00")
    event = _review_event(owner_id, world, target)
    _moderation_signal(
        target["runtime_account_id"],
        key="approve-crisis",
        at="2026-07-22 10:00:00",
        category="self_harm",
    )
    crisis = client.post(
        f"/admin/companion-world/lifecycle-events/{event['id']}/approve",
        headers=ADMIN_HEADERS,
        json={"farewell_text": "再见", "reason": "test"},
    )
    assert crisis.status_code == 409
    assert crisis.json()["detail"]["code"] == "crisis_freeze_active"

    legacy_owner, legacy_world = _world("19965001010")
    legacy_target = _resident(
        legacy_owner, legacy_world, "became-legacy", joined_at="2026-05-01 10:00:00"
    )
    _resident(legacy_owner, legacy_world, "legacy-other", joined_at="2026-07-01 10:00:00")
    legacy_event = _review_event(legacy_owner, legacy_world, legacy_target)
    with db.connect() as conn:
        conn.execute(
            "UPDATE universe_residents SET origin='legacy' WHERE id=?",
            (legacy_target["id"],),
        )
    legacy = client.post(
        f"/admin/companion-world/lifecycle-events/{legacy_event['id']}/approve",
        headers=ADMIN_HEADERS,
        json={"farewell_text": "再见", "reason": "test"},
    )
    assert legacy.status_code == 409
    assert legacy.json()["detail"]["code"] == "legacy_resident_departure_forbidden"

    last_owner, last_world = _world("19965001011")
    last = _resident(last_owner, last_world, "last", joined_at="2026-05-01 10:00:00")
    _moderation_signal(
        last["runtime_account_id"],
        key="approve-severe",
        at="2026-07-23 09:00:00",
        category="credible_threat",
    )
    severe_event = _review_event(
        last_owner,
        last_world,
        last,
        event_type="severe_abuse",
        last_resident_exception_requested=True,
    )
    protected = client.post(
        f"/admin/companion-world/lifecycle-events/{severe_event['id']}/approve",
        headers=ADMIN_HEADERS,
        json={"farewell_text": "再见", "reason": "test"},
    )
    assert protected.status_code == 409
    assert protected.json()["detail"]["code"] == "last_resident_protected"
    exception = client.post(
        f"/admin/companion-world/lifecycle-events/{severe_event['id']}/approve",
        headers=ADMIN_HEADERS,
        json={
            "farewell_text": "这段同行到这里结束，愿你平安。",
            "reason": "explicit severe abuse exception",
            "allow_last_resident_exception": True,
        },
    )
    assert exception.status_code == 200
    farewell_feed = SqlCompanionWorldRepository().list_published_posts(
        platform_user_id=last_owner,
        cursor_published_at=None,
        cursor_post_id=None,
        limit=10,
    )
    assert len(farewell_feed) == 1
    assert farewell_feed[0].post_type == "farewell"
    assert farewell_feed[0].source_type == "lifecycle_farewell"

    recovery_owner, recovery_world = _world("19965001012")
    recovered = _resident(
        recovery_owner,
        recovery_world,
        "recovered",
        joined_at="2026-05-01 10:00:00",
    )
    _resident(
        recovery_owner,
        recovery_world,
        "recovery-other",
        joined_at="2026-07-01 10:00:00",
    )
    recovery_event = _review_event(recovery_owner, recovery_world, recovered)
    _insert_inbound(
        recovered["runtime_account_id"], at="2026-07-23 09:30:00", key="recovered"
    )
    invalid = client.post(
        f"/admin/companion-world/lifecycle-events/{recovery_event['id']}/approve",
        headers=ADMIN_HEADERS,
        json={"farewell_text": "再见", "reason": "test"},
    )
    assert invalid.status_code == 409
    assert invalid.json()["detail"]["code"] == "lifecycle_evidence_invalid"


def test_offline_outbox_conflict_rolls_back_everything(fresh_db):
    owner_id, world = _world("19965001013")
    target = _resident(owner_id, world, "rollback", joined_at="2026-05-01 10:00:00")
    _resident(owner_id, world, "rollback-other", joined_at="2026-07-01 10:00:00")
    event = _review_event(owner_id, world, target)
    existing_post, _created = db.publish_user_feed_post_with_outbox(
        owner_platform_user_id=owner_id,
        client_request_id="rollback_req_001",
        text="existing",
        request_fingerprint="rollback-fingerprint",
        published_at="2026-07-23 09:30:00",
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE companion_world_outbox SET idempotency_key=? WHERE post_id=?",
            (f"departure-farewell:v1:{event['id']}", existing_post["id"]),
        )
    with pytest.raises(LifecycleCommitError) as raised:
        approve_lifecycle_event(
            event_id=event["id"],
            farewell_text="should rollback",
            reason="fault injection",
            allow_last_resident_exception=False,
            admin_user_id="admin",
            now=NOW,
            policy=build_lifecycle_policy(fresh_db),
        )
    assert raised.value.code == "lifecycle_commit_conflict"
    with db.connect() as conn:
        assert conn.execute(
            "SELECT status FROM resident_lifecycle_events WHERE id=?", (event["id"],)
        ).fetchone()["status"] == "review_pending"
        assert conn.execute(
            "SELECT status FROM universe_residents WHERE id=?", (target["id"],)
        ).fetchone()["status"] == "active"
        assert conn.execute(
            "SELECT state FROM ai_conversations WHERE resident_id=?", (target["id"],)
        ).fetchone()["state"] == "active"
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM universe_posts WHERE departure_event_id=?",
            (event["id"],),
        ).fetchone()["c"] == 0


def test_offline_uses_same_conversation_lock_as_turn(fresh_db):
    owner_id, world = _world("19965001014")
    target = _resident(owner_id, world, "locked", joined_at="2026-05-01 10:00:00")
    _resident(owner_id, world, "locked-other", joined_at="2026-07-01 10:00:00")
    event = _review_event(owner_id, world, target)
    with db.connect() as conn:
        conversation_id = conn.execute(
            "SELECT id FROM ai_conversations WHERE resident_id=?", (target["id"],)
        ).fetchone()["id"]
    with db.try_conversation_turn_lock(conversation_id) as acquired:
        assert acquired is True
        with pytest.raises(LifecycleCommitError) as raised:
            approve_lifecycle_event(
                event_id=event["id"],
                farewell_text="retry after turn",
                reason="lock test",
                allow_last_resident_exception=False,
                admin_user_id="admin",
                now=NOW,
                policy=build_lifecycle_policy(fresh_db),
            )
        assert raised.value.code == "conversation_busy"
    committed = approve_lifecycle_event(
        event_id=event["id"],
        farewell_text="turn completed",
        reason="lock retry",
        allow_last_resident_exception=False,
        admin_user_id="admin",
        now=NOW,
        policy=build_lifecycle_policy(fresh_db),
    )
    assert committed["event"]["status"] == "committed"


def test_pg_double_approve_creates_one_farewell_and_outbox(fresh_db):
    if not is_postgres():
        pytest.skip("double approve 并发正确性以 PG 为准")
    owner_id, world = _world("19965001015")
    target = _resident(owner_id, world, "double", joined_at="2026-05-01 10:00:00")
    _resident(owner_id, world, "double-other", joined_at="2026-07-01 10:00:00")
    event = _review_event(owner_id, world, target)
    barrier = threading.Barrier(2)

    def _approve(_index: int) -> bool:
        barrier.wait(timeout=5)
        return bool(
            approve_lifecycle_event(
                event_id=event["id"],
                farewell_text="one farewell",
                reason="concurrent approve",
                allow_last_resident_exception=False,
                admin_user_id="admin",
                now=NOW,
                policy=build_lifecycle_policy(fresh_db),
            )["replayed"]
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        replayed = list(executor.map(_approve, range(2)))
    assert sorted(replayed) == [False, True]
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM universe_posts WHERE departure_event_id=?",
            (event["id"],),
        ).fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM companion_world_outbox WHERE idempotency_key=?",
            (f"departure-farewell:v1:{event['id']}",),
        ).fetchone()["c"] == 1


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
