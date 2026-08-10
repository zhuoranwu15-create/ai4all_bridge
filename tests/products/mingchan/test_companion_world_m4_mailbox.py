"""M4-4 mailbox catalog、投递/expiry、owner API 与 PG 并发门禁。"""
from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import json
import threading
from datetime import datetime, timedelta

import app.db as db
import pytest
from app.bootstrap.product_registry import MINGCHAN_APP_ID, build_test_product_registry
from app.products.mingchan.domain.companion_world import (
    CompanionWorldError,
    CompanionWorldService,
    TemplateDraft,
)
from app.products.mingchan.application import SqlCompanionWorldRepository
from app.products.mingchan.application.mailbox import (
    CompanionWorldMailboxService,
    MailboxError,
    build_mailbox_policy,
)
from app.products.mingchan.jobs.world_lifecycle.scheduler import WorldLifecycleScheduler
from scripts.import_companion_world_mailbox_catalog import (
    import_mailbox_catalog,
    sign_manifest_payload,
    validate_signed_manifest,
)

ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}
STAFF_HEADERS = {"Authorization": "Bearer test-staff"}
REVIEWER_HEADERS = {"Authorization": "Bearer test-reviewer"}
NOW = datetime(2026, 7, 23, 10, 0, 0)
TEST_REGISTRY = build_test_product_registry()


def _world(phone: str) -> tuple[str, dict]:
    owner = db.create_or_get_platform_user_by_phone(
        phone=phone, display_name=f"mailbox-{phone[-4:]}"
    )
    db.ensure_product_membership(
        platform_user_id=owner["id"],
        app_id=MINGCHAN_APP_ID,
        registry=TEST_REGISTRY,
    )
    world = db.get_or_create_home_universe(platform_user_id=owner["id"])
    db.set_universe_onboarding_state(
        universe_id=world["id"], onboarding_state="confirmed"
    )
    return str(owner["id"]), db.get_universe(universe_id=world["id"])


def _template(
    name: str, *, version: str = "v1", persona_seed_json: str | None = None
) -> dict:
    return db.create_character_template(
        source_type="operations",
        name=name,
        avatar_ref=f"asset://{name}",
        summary=f"{name} summary",
        tags_json='["温柔","好奇"]',
        persona_seed_json=persona_seed_json,
        persona_version=version,
    )


def _active_resident(world: dict, name: str, *, template: dict | None = None) -> dict:
    selected = template or _template(name)
    return db.create_resident(
        universe_id=world["id"],
        character_template_id=selected["id"],
        template_version=selected["persona_version"],
        origin="preset",
        status="active",
        runtime_account_id=None,
    )


def _catalog(
    name: str,
    *,
    key: str,
    priority: int,
    policy_version: str,
    template: dict | None = None,
    version: str = "v1",
) -> dict:
    selected = template or _template(name, version=version)
    row, created = db.create_character_letter_catalog_entry(
        character_key=key,
        character_template_id=selected["id"],
        template_version=selected["persona_version"],
        letter_body=f"你好，我是{name}。",
        policy_version=policy_version,
        priority=priority,
        created_by="test",
    )
    assert created is True
    return row


def _login(client, phone: str) -> dict:
    verification = db.create_phone_verification(
        phone=phone, code="999999", expires_minutes=10
    )
    token = db.set_verification_verified(
        verification["id"], token_expires_minutes=10
    )["verified_token"]
    response = client.post(
        "/api/v1/products/mingchan/auth/session", json={"phone": phone, "verified_token": token}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _enable_mailbox(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.products.mingchan.api.mailbox.settings.mingchan_mailbox_enabled",
        True,
    )
    monkeypatch.setattr(
        "app.products.mingchan.api.world.settings.mingchan_p1_enabled", True
    )
    monkeypatch.setattr(
        "app.products.mingchan.api.mailbox.beijing_naive_now", lambda: NOW
    )


def test_delivery_capacity_catalog_selection_and_exact_cooldown(fresh_db):
    owner_id, world = _world("19966001001")
    policy = build_mailbox_policy(fresh_db)
    excluded_template = _template("already-resident")
    _active_resident(world, "existing", template=excluded_template)
    residents = [
        _active_resident(world, f"resident-{index}") for index in range(2, 9)
    ]
    _catalog(
        "excluded-high",
        key="excluded-high",
        priority=100,
        policy_version=policy.version,
        template=excluded_template,
    )
    selected = _catalog(
        "selected",
        key="selected",
        priority=20,
        policy_version=policy.version,
    )
    fallback = _catalog(
        "fallback",
        key="fallback",
        priority=10,
        policy_version=policy.version,
    )
    service = CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY)

    blocked = service.maintain_batch(
        now=NOW, after_universe_id=None, batch_size=50
    )
    assert blocked["metrics"]["blocked_capacity"] == 1
    with db.connect() as conn:
        conn.execute(
            "UPDATE universe_residents SET status='offline' WHERE id=?",
            (residents[-1]["id"],),
        )

    delivered = service.maintain_batch(
        now=NOW, after_universe_id=None, batch_size=50
    )
    assert delivered["metrics"]["delivered"] == 1
    letters = db.list_character_letters_for_owner(
        owner_platform_user_id=owner_id
    )
    assert len(letters) == 1
    assert letters[0]["catalog_id"] == selected["id"]
    assert letters[0]["eligibility_snapshot"]["active_count"] == 7
    assert letters[0]["expires_at"] == "2026-08-22 10:00:00"
    assert db.transition_open_character_letter(
        letter_id=letters[0]["id"],
        owner_platform_user_id=owner_id,
        new_status="declined",
        now="2026-07-23 11:00:00",
        terminal_reason="owner_declined",
    )["status"] == "declined"

    before = service.maintain_batch(
        now=NOW + timedelta(days=30) - timedelta(seconds=1),
        after_universe_id=None,
        batch_size=50,
    )
    assert before["metrics"]["blocked_cooldown"] == 1
    exact = service.maintain_batch(
        now=NOW + timedelta(days=30),
        after_universe_id=None,
        batch_size=50,
    )
    assert exact["metrics"]["delivered"] == 1
    latest = db.list_character_letters_for_owner(
        owner_platform_user_id=owner_id
    )[0]
    assert latest["catalog_id"] == fallback["id"]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM app_notifications").fetchone()[
            "c"
        ] == 0


def test_same_character_never_redelivers_after_new_catalog_version(fresh_db):
    owner_id, world = _world("19966001002")
    policy = build_mailbox_policy(fresh_db)
    first = _catalog(
        "traveler-v1",
        key="traveler",
        priority=10,
        policy_version=policy.version,
    )
    service = CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY)
    service.maintain_batch(now=NOW, after_universe_id=None, batch_size=50)
    letter = db.list_character_letters_for_owner(owner_platform_user_id=owner_id)[0]
    db.transition_open_character_letter(
        letter_id=letter["id"],
        owner_platform_user_id=owner_id,
        new_status="declined",
        now="2026-07-23 11:00:00",
        terminal_reason="owner_declined",
    )
    db.retire_character_letter_catalog_entry(
        catalog_id=first["id"], retired_by="test", retired_at="2026-07-24 10:00:00"
    )
    _catalog(
        "traveler-v2",
        key="traveler",
        priority=20,
        policy_version=policy.version,
        version="v2",
    )
    result = service.maintain_batch(
        now=NOW + timedelta(days=30), after_universe_id=None, batch_size=50
    )
    assert result["metrics"]["catalog_empty"] == 1
    assert len(db.list_character_letters_for_owner(owner_platform_user_id=owner_id)) == 1


def test_scheduler_expiry_exact_boundary_releases_open_slot(fresh_db):
    owner_id, _world_row = _world("19966001008")
    policy = build_mailbox_policy(fresh_db)
    _catalog("expiry-first", key="expiry-first", priority=20, policy_version=policy.version)
    _catalog("expiry-second", key="expiry-second", priority=10, policy_version=policy.version)
    service = CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY)
    service.maintain_batch(now=NOW, after_universe_id=None, batch_size=50)
    before = db.list_character_letters_for_owner(owner_platform_user_id=owner_id)
    assert len(before) == 1 and before[0]["status"] == "unread"

    exact = service.maintain_batch(
        now=NOW + timedelta(days=30), after_universe_id=None, batch_size=50
    )
    assert exact["metrics"]["expired"] == 1
    assert exact["metrics"]["delivered"] == 1
    after = db.list_character_letters_for_owner(owner_platform_user_id=owner_id)
    assert [item["status"] for item in after] == ["unread", "expired"]


def test_catalog_new_version_excludes_existing_resident_old_version(fresh_db):
    owner_id, world = _world("19966001009")
    policy = build_mailbox_policy(fresh_db)
    old_template = _template("known-v1", version="v1")
    old_catalog = _catalog(
        "known-v1",
        key="known-character",
        priority=10,
        policy_version=policy.version,
        template=old_template,
    )
    _active_resident(world, "known-resident", template=old_template)
    db.retire_character_letter_catalog_entry(
        catalog_id=old_catalog["id"],
        retired_by="test",
        retired_at="2026-07-22 10:00:00",
    )
    _catalog(
        "known-v2",
        key="known-character",
        priority=20,
        policy_version=policy.version,
        version="v2",
    )
    result = CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY).maintain_batch(
        now=NOW, after_universe_id=None, batch_size=50
    )
    assert result["metrics"]["catalog_empty"] == 1
    assert db.list_character_letters_for_owner(owner_platform_user_id=owner_id) == []


def test_owner_api_is_private_no_auto_read_and_request_time_expiry(
    client, fresh_db, monkeypatch
):
    phone = "19966001003"
    owner_id, world = _world(phone)
    policy = build_mailbox_policy(fresh_db)
    _catalog("private", key="private", priority=10, policy_version=policy.version)
    CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY).maintain_batch(
        now=NOW - timedelta(days=30), after_universe_id=None, batch_size=50
    )
    letter = db.list_character_letters_for_owner(owner_platform_user_id=owner_id)[0]
    headers = _login(client, phone)
    disabled = client.get("/api/v1/products/mingchan/mailbox/letters", headers=headers)
    assert disabled.status_code == 404
    _enable_mailbox(monkeypatch)

    listed = client.get("/api/v1/products/mingchan/mailbox/letters", headers=headers)
    unread = client.get("/api/v1/products/mingchan/mailbox/unread-count", headers=headers)
    assert listed.status_code == unread.status_code == 200
    item = listed.json()["data"]["items"][0]
    assert item["status"] == "expired"
    assert unread.json()["data"]["unread_count"] == 0
    assert listed.headers["cache-control"] == "no-store"
    assert "catalog_id" not in listed.text
    assert "owner_platform_user_id" not in listed.text
    assert "policy_version" not in listed.text
    assert "eligibility_snapshot" not in listed.text
    assert "runtime_account_id" not in listed.text
    expired_action = client.post(
        f"/api/v1/products/mingchan/mailbox/letters/{letter['id']}/read", headers=headers, json={}
    )
    assert expired_action.status_code == 409
    assert expired_action.json()["code"] == "letter_not_open"

    other_phone = "19966001004"
    _world(other_phone)
    other_headers = _login(client, other_phone)
    hidden = client.get(
        f"/api/v1/products/mingchan/mailbox/letters/{letter['id']}", headers=other_headers
    )
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "letter_not_found"


def test_owner_read_defer_decline_are_idempotent_and_do_not_extend_ttl(
    client, fresh_db, monkeypatch
):
    phone = "19966001005"
    owner_id, _world_row = _world(phone)
    policy = build_mailbox_policy(fresh_db)
    _catalog("actions", key="actions", priority=10, policy_version=policy.version)
    CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY).maintain_batch(
        now=NOW, after_universe_id=None, batch_size=50
    )
    letter = db.list_character_letters_for_owner(owner_platform_user_id=owner_id)[0]
    headers = _login(client, phone)
    _enable_mailbox(monkeypatch)
    path = f"/api/v1/products/mingchan/mailbox/letters/{letter['id']}"

    read = client.post(f"{path}/read", headers=headers, json={})
    replay_read = client.post(f"{path}/read", headers=headers, json={})
    deferred = client.post(f"{path}/defer", headers=headers, json={})
    replay_defer = client.post(f"{path}/defer", headers=headers, json={})
    declined = client.post(f"{path}/decline", headers=headers, json={})
    replay_decline = client.post(f"{path}/decline", headers=headers, json={})
    assert all(
        response.status_code == 200
        for response in (read, replay_read, deferred, replay_defer, declined, replay_decline)
    )
    assert read.json()["data"]["letter"]["status"] == "read"
    assert deferred.json()["data"]["letter"]["status"] == "deferred"
    assert declined.json()["data"]["letter"]["status"] == "declined"
    assert deferred.json()["data"]["letter"]["expires_at"] == read.json()["data"][
        "letter"
    ]["expires_at"]
    second_catalog = _catalog(
        "actions-two",
        key="actions-two",
        priority=5,
        policy_version=policy.version,
    )
    second, _created = db.insert_character_letter(
        owner_platform_user_id=owner_id,
        universe_id=letter["universe_id"],
        catalog_id=second_catalog["id"],
        idempotency_key=f"actions-second:{owner_id}",
        request_fingerprint="actions-second-fingerprint",
        eligibility_snapshot={"active_count": 0},
        policy_version=policy.version,
        delivered_at="2026-07-23 10:00:00",
        expires_at="2026-08-22 10:00:00",
    )
    first_page = client.get("/api/v1/products/mingchan/mailbox/letters?limit=1", headers=headers)
    cursor = first_page.json()["data"]["next_cursor"]
    second_page = client.get(
        "/api/v1/products/mingchan/mailbox/letters", headers=headers, params={"limit": 1, "cursor": cursor}
    )
    assert {
        first_page.json()["data"]["items"][0]["letter_id"],
        second_page.json()["data"]["items"][0]["letter_id"],
    } == {letter["id"], second["id"]}
    assert client.get(
        "/api/v1/products/mingchan/mailbox/letters?cursor=not-base64!", headers=headers
    ).status_code == 400
    injected = client.post(
        f"{path}/read", headers=headers, json={"platform_user_id": "other"}
    )
    assert injected.status_code == 422


def test_admin_catalog_permissions_create_replay_and_retire(client, fresh_db):
    policy = build_mailbox_policy(fresh_db)
    template = _template("admin-catalog")
    payload = {
        "character_key": "admin-catalog",
        "character_template_id": template["id"],
        "template_version": "v1",
        "letter_body": "一封由运营审核过的来信。",
        "priority": 20,
    }
    assert client.get(
        "/admin/products/mingchan/world/mailbox/catalog", headers=REVIEWER_HEADERS
    ).status_code == 403
    created = client.post(
        "/admin/products/mingchan/world/mailbox/catalog", headers=STAFF_HEADERS, json=payload
    )
    replay = client.post(
        "/admin/products/mingchan/world/mailbox/catalog", headers=ADMIN_HEADERS, json=payload
    )
    assert created.status_code == 201 and replay.status_code == 200
    entry = created.json()["entry"]
    assert entry["policy_version"] == policy.version
    assert replay.json()["entry"]["id"] == entry["id"]
    assert created.headers["cache-control"] == "no-store"
    changed = client.post(
        "/admin/products/mingchan/world/mailbox/catalog",
        headers=STAFF_HEADERS,
        json={**payload, "letter_body": "静默改写"},
    )
    assert changed.status_code == 422
    listed = client.get(
        "/admin/products/mingchan/world/mailbox/catalog", headers=STAFF_HEADERS
    )
    assert listed.status_code == 200 and len(listed.json()["entries"]) == 1
    retired = client.post(
        f"/admin/products/mingchan/world/mailbox/catalog/{entry['id']}/retire",
        headers=STAFF_HEADERS,
        json={"reason": "version rollout"},
    )
    assert retired.status_code == 200
    assert retired.json()["entry"]["status"] == "retired"


def _manifest(template: dict, policy_version: str, *, version: str = "v1") -> dict:
    return {
        "version": 1,
        "entries": [
            {
                "catalog_id": f"lcat_manifest_{version}",
                "character_key": "manifest-character",
                "character_template_id": template["id"],
                "template_version": template["persona_version"],
                "letter_body": f"manifest {version} letter",
                "policy_version": policy_version,
                "priority": 10,
            }
        ],
    }


def test_signed_manifest_dry_run_apply_tamper_and_version_roll(fresh_db):
    secret = "test-mailbox-manifest-secret"
    policy = build_mailbox_policy(fresh_db)
    template_v1 = _template("manifest-v1", version="v1")
    payload = _manifest(template_v1, policy.version)
    payload["signature"] = sign_manifest_payload(payload, secret=secret)
    records = validate_signed_manifest(payload, secret=secret)
    tampered = copy.deepcopy(payload)
    tampered["entries"][0]["letter_body"] = "tampered"
    with pytest.raises(ValueError, match="signature"):
        validate_signed_manifest(tampered, secret=secret)

    dry = import_mailbox_catalog(records, dry_run=True, actor="test")
    assert dry.create_ids == ["lcat_manifest_v1"]
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM character_letter_catalog"
        ).fetchone()["c"] == 0
    applied = import_mailbox_catalog(records, dry_run=False, actor="test")
    replay = import_mailbox_catalog(records, dry_run=False, actor="test")
    assert applied.catalog_ready is True
    assert replay.keep_ids == ["lcat_manifest_v1"]

    template_v2 = _template("manifest-v2", version="v2")
    payload_v2 = _manifest(template_v2, policy.version, version="v2")
    payload_v2["signature"] = sign_manifest_payload(payload_v2, secret=secret)
    rolled = import_mailbox_catalog(
        validate_signed_manifest(payload_v2, secret=secret),
        dry_run=False,
        actor="test",
    )
    assert rolled.retire_ids == ["lcat_manifest_v1"]
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, status FROM character_letter_catalog ORDER BY id"
        ).fetchall()
    assert [(row["id"], row["status"]) for row in rows] == [
        ("lcat_manifest_v1", "retired"),
        ("lcat_manifest_v2", "active"),
    ]


def test_scheduler_can_run_mailbox_with_lifecycle_disabled(fresh_db):
    owner_id, _world_row = _world("19966001006")
    policy = build_mailbox_policy(fresh_db)
    _catalog("scheduler", key="scheduler", priority=10, policy_version=policy.version)
    scheduler = WorldLifecycleScheduler(
        enabled=False,
        mailbox_enabled=True,
        interval_seconds=300,
        batch_size=50,
        mailbox_service=CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY),
    )
    result = asyncio.run(scheduler.run_once(now=NOW))
    assert result["metrics"] is None
    assert result["mailbox_metrics"]["delivered"] == 1
    scheduler._heartbeat("ok")
    heartbeat = db.get_scheduler_heartbeat("world_lifecycle_scheduler")
    assert heartbeat["metadata"]["mailbox_enabled"] is True
    assert heartbeat["metadata"]["last_run_mailbox_metrics"]["delivered"] == 1
    assert len(db.list_character_letters_for_owner(owner_platform_user_id=owner_id)) == 1


def test_accept_api_creates_minimal_runtime_and_replays_same_resident(
    client, fresh_db, monkeypatch
):
    phone = "19966001010"
    owner_id, _world_row = _world(phone)
    policy = build_mailbox_policy(fresh_db)
    template = _template(
        "accepted",
        persona_seed_json=json.dumps(
            {
                "SOUL.md": "# SOUL\n\n温柔但独立。",
                "IDENTITY.md": "# IDENTITY\n\n名字是 accepted。",
                "system_prompt": "保持自然。",
            },
            ensure_ascii=False,
        ),
    )
    _catalog(
        "accepted",
        key="accepted",
        priority=10,
        policy_version=policy.version,
        template=template,
    )
    CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY).maintain_batch(
        now=NOW, after_universe_id=None, batch_size=50
    )
    letter = db.list_character_letters_for_owner(owner_platform_user_id=owner_id)[0]
    headers = _login(client, phone)
    _enable_mailbox(monkeypatch)
    tracked_tables = (
        "accounts",
        "profiles",
        "account_profile_files",
        "universe_residents",
        "ai_conversations",
        "account_owner_bindings",
        "subscriptions",
        "entitlement_wallets",
        "entitlement_ledger",
        "app_notifications",
        "companion_world_outbox",
    )
    with db.connect() as conn:
        before = {
            table: conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()[
                "c"
            ]
            for table in tracked_tables
        }

    accepted = client.post(
        f"/api/v1/products/mingchan/mailbox/letters/{letter['id']}/accept", headers=headers, json={}
    )
    replay = client.post(
        f"/api/v1/products/mingchan/mailbox/letters/{letter['id']}/accept", headers=headers, json={}
    )
    assert accepted.status_code == replay.status_code == 200
    assert accepted.json()["data"]["replayed"] is False
    assert replay.json()["data"]["replayed"] is True
    assert accepted.json()["data"]["resident"] == replay.json()["data"]["resident"]
    assert accepted.json()["data"]["letter"]["status"] == "accepted"
    assert accepted.headers["cache-control"] == "no-store"
    for private_field in (
        "runtime_account_id",
        "owner_platform_user_id",
        "catalog_id",
        "persona_seed_json",
        "eligibility_snapshot",
    ):
        assert private_field not in accepted.text

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"] == before[
            "accounts"
        ] + 1
        assert conn.execute("SELECT COUNT(*) AS c FROM profiles").fetchone()["c"] == before[
            "profiles"
        ] + 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM account_profile_files"
        ).fetchone()["c"] == before["account_profile_files"] + 2
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM universe_residents WHERE origin='mailbox'"
        ).fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM ai_conversations"
        ).fetchone()["c"] == before["ai_conversations"] + 1
        for table in (
            "account_owner_bindings",
            "subscriptions",
            "entitlement_wallets",
            "entitlement_ledger",
            "app_notifications",
            "companion_world_outbox",
        ):
            assert conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()[
                "c"
            ] == before[table]

    other_phone = "19966001011"
    _world(other_phone)
    hidden = client.post(
        f"/api/v1/products/mingchan/mailbox/letters/{letter['id']}/accept",
        headers=_login(client, other_phone),
        json={},
    )
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "letter_not_found"


def test_accept_exact_expiry_commits_expired_status(fresh_db):
    owner_id, _world_row = _world("19966001012")
    policy = build_mailbox_policy(fresh_db)
    _catalog("exact-expiry", key="exact-expiry", priority=10, policy_version=policy.version)
    service = CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY)
    service.maintain_batch(now=NOW, after_universe_id=None, batch_size=50)
    letter = db.list_character_letters_for_owner(owner_platform_user_id=owner_id)[0]

    with pytest.raises(MailboxError) as raised:
        service.accept_letter(
            owner_id, letter_id=letter["id"], now="2026-08-22 10:00:00"
        )
    assert raised.value.code == "letter_expired"
    persisted = db.get_character_letter_for_owner(
        letter_id=letter["id"], owner_platform_user_id=owner_id
    )
    assert persisted["status"] == "expired"
    assert persisted["handled_at"] == "2026-08-22 10:00:00"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"] == 0


@pytest.mark.parametrize("unavailable", ["catalog", "template"])
def test_accept_revalidates_catalog_and_template_without_orphans(
    fresh_db, unavailable
):
    owner_id, _world_row = _world(f"1996600101{3 if unavailable == 'catalog' else 4}")
    policy = build_mailbox_policy(fresh_db)
    template = _template(f"unavailable-{unavailable}")
    catalog = _catalog(
        f"unavailable-{unavailable}",
        key=f"unavailable-{unavailable}",
        priority=10,
        policy_version=policy.version,
        template=template,
    )
    service = CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY)
    service.maintain_batch(now=NOW, after_universe_id=None, batch_size=50)
    letter = db.list_character_letters_for_owner(owner_platform_user_id=owner_id)[0]
    if unavailable == "catalog":
        db.retire_character_letter_catalog_entry(
            catalog_id=catalog["id"],
            retired_by="test",
            retired_at="2026-07-23 10:01:00",
        )
    else:
        with db.connect() as conn:
            conn.execute(
                "UPDATE character_templates SET status='retired' WHERE id=?",
                (template["id"],),
            )

    with pytest.raises(MailboxError) as raised:
        service.accept_letter(
            owner_id, letter_id=letter["id"], now="2026-07-23 10:02:00"
        )
    assert raised.value.code == "letter_template_unavailable"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM universe_residents"
        ).fetchone()["c"] == 0
    assert db.get_character_letter_for_owner(
        letter_id=letter["id"], owner_platform_user_id=owner_id
    )["status"] == "unread"


def test_accept_capacity_ten_blocks_without_orphans(fresh_db):
    owner_id, world = _world("19966001015")
    policy = build_mailbox_policy(fresh_db)
    _catalog("full", key="full", priority=10, policy_version=policy.version)
    service = CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY)
    service.maintain_batch(now=NOW, after_universe_id=None, batch_size=50)
    letter = db.list_character_letters_for_owner(owner_platform_user_id=owner_id)[0]
    for index in range(10):
        _active_resident(world, f"capacity-{index}")

    with pytest.raises(MailboxError) as raised:
        service.accept_letter(
            owner_id, letter_id=letter["id"], now="2026-07-23 10:01:00"
        )
    assert raised.value.code == "resident_capacity_exceeded"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM universe_residents WHERE origin='mailbox'"
        ).fetchone()["c"] == 0


def test_accept_failure_after_runtime_insert_rolls_back_everything(
    fresh_db, monkeypatch
):
    owner_id, _world_row = _world("19966001016")
    policy = build_mailbox_policy(fresh_db)
    template = _template(
        "rollback",
        persona_seed_json=json.dumps(
            {"SOUL.md": "rollback soul", "IDENTITY.md": "rollback identity"}
        ),
    )
    _catalog(
        "rollback",
        key="rollback",
        priority=10,
        policy_version=policy.version,
        template=template,
    )
    service = CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY)
    service.maintain_batch(now=NOW, after_universe_id=None, batch_size=50)
    letter = db.list_character_letters_for_owner(owner_platform_user_id=owner_id)[0]

    def _fail_create_resident(**_kwargs):
        raise RuntimeError("injected resident failure")

    monkeypatch.setattr(
        "app.products.mingchan.application.mailbox.create_resident",
        _fail_create_resident,
    )
    with pytest.raises(RuntimeError, match="injected resident failure"):
        service.accept_letter(
            owner_id, letter_id=letter["id"], now="2026-07-23 10:01:00"
        )
    with db.connect() as conn:
        for table in (
            "accounts",
            "profiles",
            "account_profile_files",
            "universe_residents",
            "ai_conversations",
        ):
            assert conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()[
                "c"
            ] == 0
    assert db.get_character_letter_for_owner(
        letter_id=letter["id"], owner_platform_user_id=owner_id
    )["status"] == "unread"


def test_pg_concurrent_mailbox_delivery_keeps_one_open_letter(fresh_db):
    owner_id, _world_row = _world("19966001007")
    policy = build_mailbox_policy(fresh_db)
    _catalog("concurrent", key="concurrent", priority=10, policy_version=policy.version)
    barrier = threading.Barrier(2)

    def _run(_index: int) -> str:
        service = CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY)
        barrier.wait(timeout=5)
        result = service.maintain_batch(
            now=NOW, after_universe_id=None, batch_size=50
        )
        return result["results"][0]["status"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(_run, range(2)))
    assert sorted(statuses) == ["blocked_open", "delivered"]
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM character_letters "
            "WHERE owner_platform_user_id=? AND status IN ('unread','read','deferred')",
            (owner_id,),
        ).fetchone()["c"] == 1


def test_pg_double_accept_replays_one_runtime_resident_and_conversation(fresh_db):
    owner_id, _world_row = _world("19966001017")
    policy = build_mailbox_policy(fresh_db)
    _catalog("double-accept", key="double-accept", priority=10, policy_version=policy.version)
    CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY).maintain_batch(
        now=NOW, after_universe_id=None, batch_size=50
    )
    letter = db.list_character_letters_for_owner(owner_platform_user_id=owner_id)[0]
    barrier = threading.Barrier(2)

    def _accept(_index: int) -> tuple[bool, str, str]:
        barrier.wait(timeout=5)
        result = CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY).accept_letter(
            owner_id, letter_id=letter["id"], now="2026-07-23 10:01:00"
        )
        return (
            bool(result["replayed"]),
            str(result["resident"]["resident_id"]),
            str(result["resident"]["conversation_id"]),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_accept, range(2)))
    assert sorted(item[0] for item in results) == [False, True]
    assert len({item[1] for item in results}) == 1
    assert len({item[2] for item in results}) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM universe_residents WHERE origin='mailbox'"
        ).fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM ai_conversations"
        ).fetchone()["c"] == 1


def test_pg_mailbox_accept_and_regular_create_share_tenth_slot(fresh_db):
    owner_id, world = _world("19966001018")
    policy = build_mailbox_policy(fresh_db)
    _catalog("race-capacity", key="race-capacity", priority=10, policy_version=policy.version)
    CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY).maintain_batch(
        now=NOW, after_universe_id=None, batch_size=50
    )
    letter = db.list_character_letters_for_owner(owner_platform_user_id=owner_id)[0]
    for index in range(9):
        _active_resident(world, f"race-existing-{index}")
    barrier = threading.Barrier(2)

    def _accept() -> str:
        barrier.wait(timeout=5)
        try:
            CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY).accept_letter(
                owner_id, letter_id=letter["id"], now="2026-07-23 10:01:00"
            )
            return "created"
        except MailboxError as err:
            return err.code

    def _regular_create() -> str:
        barrier.wait(timeout=5)
        try:
            CompanionWorldService(SqlCompanionWorldRepository()).create_resident(
                owner_id,
                custom_template=TemplateDraft(
                    name="regular-tenth",
                    persona_seed_json=json.dumps(
                        {
                            "SOUL.md": "regular soul",
                            "IDENTITY.md": "regular identity",
                        }
                    ),
                ),
            )
            return "created"
        except CompanionWorldError as err:
            return err.code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        accept_future = executor.submit(_accept)
        create_future = executor.submit(_regular_create)
        results = [
            accept_future.result(timeout=10),
            create_future.result(timeout=10),
        ]
    assert sorted(results) == ["created", "resident_capacity_exceeded"]
    assert db.count_active_residents(universe_id=world["id"]) == 10


def test_pg_accept_vs_expiry_is_never_torn(fresh_db):
    owner_id, _world_row = _world("19966001019")
    policy = build_mailbox_policy(fresh_db)
    _catalog("race-expiry", key="race-expiry", priority=10, policy_version=policy.version)
    CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY).maintain_batch(
        now=NOW, after_universe_id=None, batch_size=50
    )
    letter = db.list_character_letters_for_owner(owner_platform_user_id=owner_id)[0]
    barrier = threading.Barrier(2)

    def _accept() -> str:
        barrier.wait(timeout=5)
        try:
            CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY).accept_letter(
                owner_id, letter_id=letter["id"], now="2026-08-22 09:59:59"
            )
            return "accepted"
        except MailboxError as err:
            return err.code

    def _expire() -> str:
        barrier.wait(timeout=5)
        changed = db.expire_due_character_letters(
            owner_platform_user_id=owner_id,
            now="2026-08-22 10:00:00",
        )
        return "expired" if changed == 1 else "unchanged"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        accept_future = executor.submit(_accept)
        expiry_future = executor.submit(_expire)
        outcomes = {accept_future.result(timeout=10), expiry_future.result(timeout=10)}
    assert outcomes in (
        {"accepted", "unchanged"},
        {"letter_expired", "expired"},
    )
    persisted = db.get_character_letter_for_owner(
        letter_id=letter["id"], owner_platform_user_id=owner_id
    )
    with db.connect() as conn:
        resident_count = conn.execute(
            "SELECT COUNT(*) AS c FROM universe_residents WHERE origin='mailbox'"
        ).fetchone()["c"]
        conversation_count = conn.execute(
            "SELECT COUNT(*) AS c FROM ai_conversations"
        ).fetchone()["c"]
    if persisted["status"] == "accepted":
        assert resident_count == conversation_count == 1
    else:
        assert persisted["status"] == "expired"
        assert resident_count == conversation_count == 0


def test_pg_accept_vs_catalog_retire_is_never_torn(fresh_db):
    owner_id, _world_row = _world("19966001020")
    policy = build_mailbox_policy(fresh_db)
    catalog = _catalog(
        "race-retire",
        key="race-retire",
        priority=10,
        policy_version=policy.version,
    )
    CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY).maintain_batch(
        now=NOW, after_universe_id=None, batch_size=50
    )
    letter = db.list_character_letters_for_owner(owner_platform_user_id=owner_id)[0]
    barrier = threading.Barrier(2)

    def _accept() -> str:
        barrier.wait(timeout=5)
        try:
            CompanionWorldMailboxService(policy=policy, registry=TEST_REGISTRY).accept_letter(
                owner_id, letter_id=letter["id"], now="2026-07-23 10:01:00"
            )
            return "accepted"
        except MailboxError as err:
            return err.code

    def _retire() -> str:
        barrier.wait(timeout=5)
        retired = db.retire_character_letter_catalog_entry(
            catalog_id=catalog["id"],
            retired_by="concurrency-test",
            retired_at="2026-07-23 10:01:00",
        )
        return str(retired["status"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        accept_future = executor.submit(_accept)
        retire_future = executor.submit(_retire)
        accept_outcome = accept_future.result(timeout=10)
        retire_outcome = retire_future.result(timeout=10)
    assert retire_outcome == "retired"
    assert accept_outcome in {"accepted", "letter_template_unavailable"}

    persisted = db.get_character_letter_for_owner(
        letter_id=letter["id"], owner_platform_user_id=owner_id
    )
    with db.connect() as conn:
        resident_count = conn.execute(
            "SELECT COUNT(*) AS c FROM universe_residents WHERE origin='mailbox'"
        ).fetchone()["c"]
        conversation_count = conn.execute(
            "SELECT COUNT(*) AS c FROM ai_conversations"
        ).fetchone()["c"]
    if accept_outcome == "accepted":
        assert persisted["status"] == "accepted"
        assert resident_count == conversation_count == 1
    else:
        assert persisted["status"] == "unread"
        assert resident_count == conversation_count == 0
@pytest.fixture
def client(mingchan_client):
    return mingchan_client
