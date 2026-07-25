"""Companion World 的 PG 权威并发门禁（SQLite 单写者不作数）。"""
import concurrent.futures
import json
import threading

import pytest

import app.db as db
from app.db._backend import is_postgres
from app.products.zhaoxi.domain.companion_world import (
    CompanionWorldError,
    CompanionWorldService,
    ResidentSelection,
    TemplateDraft,
    user_post_fingerprint,
)
from app.products.zhaoxi.application import SqlCompanionWorldRepository
from tests.factories import make_resident_account


def _draft(name: str) -> TemplateDraft:
    return TemplateDraft(
        name=name,
        persona_seed_json=json.dumps(
            {"SOUL.md": f"# SOUL\n\n{name}", "IDENTITY.md": f"# IDENTITY\n\n{name}"},
            ensure_ascii=False,
        ),
    )


def test_concurrent_tenth_and_eleventh_resident_only_one_commits(fresh_db):
    if not is_postgres():
        pytest.skip("world row lock 并发正确性以 PG 为准")

    user_id = db.create_or_get_platform_user_by_phone(
        phone="19950002001", display_name="并发用户"
    )["id"]
    for rank in range(1, 5):
        db.create_character_template(
            source_type="operations",
            name=f"首发{rank}",
            avatar_ref=f"asset://{rank}",
            summary=f"简介{rank}",
            tags_json=json.dumps(["a", "b", "c"]),
            persona_seed_json=json.dumps(
                {"SOUL.md": f"soul-{rank}", "IDENTITY.md": f"identity-{rank}"}
            ),
            persona_version=f"v{rank}",
            initial_candidate_rank=rank,
        )
    service = CompanionWorldService(SqlCompanionWorldRepository())
    boot = service.bootstrap_home(user_id)
    service.confirm_residents(
        user_id,
        [ResidentSelection(item.template.id) for item in boot.candidates],
    )
    for index in range(5, 10):
        service.create_resident(user_id, custom_template=_draft(f"居民{index}"))
    assert len(service.list_residents(user_id)) == 9

    barrier = threading.Barrier(2)

    def _create(index: int) -> str:
        local = CompanionWorldService(SqlCompanionWorldRepository())
        barrier.wait(timeout=5)
        try:
            local.create_resident(
                user_id, custom_template=_draft(f"并发居民{index}")
            )
            return "created"
        except CompanionWorldError as err:
            return err.code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_create, (10, 11)))

    assert sorted(results) == ["created", "resident_capacity_exceeded"]
    assert len(service.list_residents(user_id)) == 10
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 10
        assert conn.execute(
            "SELECT COUNT(*) c FROM character_templates WHERE name LIKE '并发居民%'"
        ).fetchone()["c"] == 1


def test_two_residents_concurrently_append_l3_without_overwrite(fresh_db):
    if not is_postgres():
        pytest.skip("L3 多 writer append 并发正确性以 PG 为准")

    user_id = db.create_or_get_platform_user_by_phone(
        phone="19950002002", display_name="L3 并发用户"
    )["id"]
    account_a = make_resident_account(user_id, "L3居民A")
    account_b = make_resident_account(user_id, "L3居民B")
    scope_a = db.resolve_resident_memory_scope(runtime_account_id=account_a)
    scope_b = db.resolve_resident_memory_scope(runtime_account_id=account_b)
    assert scope_a["universe_id"] == scope_b["universe_id"]
    universe_id = scope_a["universe_id"]
    barrier = threading.Barrier(2)

    def _append(account_id: str, resident_id: str, marker: str) -> str:
        barrier.wait(timeout=5)
        return db.append_universe_fact(
            universe_id=universe_id,
            fact_type="user_event",
            payload_json=json.dumps({"marker": marker}),
            source_account_id=account_id,
            source_resident_id=resident_id,
            source_message_id=f"message-{marker}",
            occurred_at="2026-07-22T10:00:00+08:00",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(
                _append, account_a, scope_a["resident_id"], "a"
            ),
            executor.submit(
                _append, account_b, scope_b["resident_id"], "b"
            ),
        )
        fact_ids = {future.result(timeout=10) for future in futures}

    facts = db.read_universe_facts(universe_id=universe_id)
    assert {item["id"] for item in facts} == fact_ids
    assert {json.loads(item["payload_json"])["marker"] for item in facts} == {
        "a",
        "b",
    }


def _m3_world(phone: str, name: str):
    user_id = db.create_or_get_platform_user_by_phone(
        phone=phone, display_name=name
    )["id"]
    account_id = make_resident_account(user_id, name)
    scope = db.resolve_resident_memory_scope(runtime_account_id=account_id)
    db.set_universe_onboarding_state(
        universe_id=scope["universe_id"], onboarding_state="confirmed"
    )
    return user_id, scope


def test_m3_concurrent_ai_slot_claim_creates_one_row(fresh_db):
    if not is_postgres():
        pytest.skip("AI slot claim 并发正确性以 PG 为准")

    _, scope = _m3_world("19950002003", "Feed 并发用户")
    barrier = threading.Barrier(4)

    def _claim(index: int):
        barrier.wait(timeout=5)
        return db.claim_ai_feed_slot(
            universe_id=scope["universe_id"],
            author_resident_id=scope["resident_id"],
            ai_local_date="2026-07-22",
            ai_slot="morning",
            slot_window_end_at="2026-07-22 11:00:00",
            claim_token=f"slot-worker-{index}",
            claimed_at="2026-07-22 09:00:00",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(_claim, range(4)))

    assert sum(1 for _, created in results if created) == 1
    assert len({row["id"] for row, _ in results if row is not None}) == 1


def test_m3_concurrent_human_reservation_is_one_per_platform_user(fresh_db):
    if not is_postgres():
        pytest.skip("真人级 reservation 并发正确性以 PG 为准")

    user_id, scope = _m3_world("19950002004", "通知并发用户")
    barrier = threading.Barrier(2)

    def _reserve(index: int):
        barrier.wait(timeout=5)
        return db.reserve_human_app_notification(
            platform_user_id=user_id,
            universe_id=scope["universe_id"],
            category="content_invitation",
            source_type="account_check",
            source_id=f"due-{index}",
            idempotency_key=f"human-proactive:v1:content_invitation:due-{index}",
            request_fingerprint=f"fingerprint-{index}",
            claim_token=f"reservation-worker-{index}",
            claim_expires_at="2026-07-22 10:05:00",
            now="2026-07-22 10:00:00",
            visible_since="2026-07-21 10:00:00",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_reserve, range(2)))

    assert sum(1 for row, created in results if row is not None and created) == 1
    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM app_notifications "
            "WHERE platform_user_id = ? AND delivery_status = 'reserved'",
            (user_id,),
        ).fetchone()["c"]
    assert int(count) == 1


def test_m3_concurrent_outbox_workers_claim_disjoint_rows(fresh_db):
    if not is_postgres():
        pytest.skip("outbox SKIP LOCKED 并发正确性以 PG 为准")

    _, scope = _m3_world("19950002005", "Outbox 并发用户")
    for index, (date, slot) in enumerate(
        (
            ("2026-07-21", "morning"),
            ("2026-07-21", "evening"),
            ("2026-07-22", "morning"),
            ("2026-07-22", "evening"),
        )
    ):
        end_at = f"{date} 11:00:00" if slot == "morning" else f"{date} 21:00:00"
        claimed_at = f"{date} 09:00:00" if slot == "morning" else f"{date} 19:00:00"
        published_at = f"{date} 09:30:00" if slot == "morning" else f"{date} 19:30:00"
        post, created = db.claim_ai_feed_slot(
            universe_id=scope["universe_id"],
            author_resident_id=scope["resident_id"],
            ai_local_date=date,
            ai_slot=slot,
            slot_window_end_at=end_at,
            claim_token=f"seed-{index}",
            claimed_at=claimed_at,
        )
        assert created is True
        db.publish_ai_feed_post_with_outbox(
            post_id=post["id"],
            claim_token=post["claim_token"],
            text=f"动态-{index}",
            published_at=published_at,
            outbox_idempotency_key=f"world-post-published:v1:{post['id']}",
            payload={"post_id": post["id"]},
        )

    barrier = threading.Barrier(2)

    def _claim_outbox(index: int):
        barrier.wait(timeout=5)
        return db.claim_companion_world_outbox(
            batch_size=2,
            now="2026-07-22 22:00:00",
            claim_token=f"outbox-worker-{index}",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        batches = list(executor.map(_claim_outbox, range(2)))

    first_ids = {row["id"] for row in batches[0]}
    second_ids = {row["id"] for row in batches[1]}
    assert len(first_ids) == len(second_ids) == 2
    assert first_ids.isdisjoint(second_ids)


def test_m3_concurrent_user_feed_replay_creates_one_post_and_outbox(fresh_db):
    if not is_postgres():
        pytest.skip("用户 Feed 幂等竞争正确性以 PG 为准")

    user_id, _scope = _m3_world("19950002006", "用户 Feed 并发")
    barrier = threading.Barrier(4)
    text = "并发重试也只能发布一次"

    def _publish(index: int):
        barrier.wait(timeout=5)
        return db.publish_user_feed_post_with_outbox(
            owner_platform_user_id=user_id,
            client_request_id="feed_concurrent_0001",
            text=text,
            request_fingerprint=user_post_fingerprint(text),
            published_at=f"2026-07-22 12:00:0{index}",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(_publish, range(4)))

    assert sum(1 for _row, created in results if created) == 1
    assert len({row["id"] for row, _created in results}) == 1
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM universe_posts WHERE author_platform_user_id = ?",
            (user_id,),
        ).fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM companion_world_outbox"
        ).fetchone()["c"] == 1


def test_m3_concurrent_stale_ai_slot_reclaim_has_one_winner(fresh_db):
    if not is_postgres():
        pytest.skip("AI slot stale lease 重领正确性以 PG 为准")

    _user_id, scope = _m3_world("19950002007", "Slot 重领并发")
    original, created = db.claim_ai_feed_slot(
        universe_id=scope["universe_id"],
        author_resident_id=scope["resident_id"],
        ai_local_date="2026-07-22",
        ai_slot="morning",
        slot_window_end_at="2026-07-22 11:00:00",
        claim_token="original-slot-worker",
        claimed_at="2026-07-22 09:00:00",
    )
    assert created is True and original["attempt_count"] == 1
    barrier = threading.Barrier(2)

    def _reclaim(index: int):
        barrier.wait(timeout=5)
        return db.claim_ai_feed_slot(
            universe_id=scope["universe_id"],
            author_resident_id=scope["resident_id"],
            ai_local_date="2026-07-22",
            ai_slot="morning",
            slot_window_end_at="2026-07-22 11:00:00",
            claim_token=f"reclaim-worker-{index}",
            claimed_at="2026-07-22 09:10:00",
            stale_before="2026-07-22 09:05:00",
            max_attempts=3,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_reclaim, range(2)))

    assert sum(1 for _row, acquired in results if acquired) == 1
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM universe_posts WHERE id=?", (original["id"],)
        ).fetchone()
    assert row["attempt_count"] == 2


def test_m3_concurrent_visible_notifications_keep_owner_limit(fresh_db):
    if not is_postgres():
        pytest.skip("通知真人锁与 200 条上限并发正确性以 PG 为准")

    user_id, scope = _m3_world("19950002008", "通知上限并发")
    barrier = threading.Barrier(2)

    def _insert(index: int):
        barrier.wait(timeout=5)
        return db.insert_visible_app_notification(
            platform_user_id=user_id,
            universe_id=scope["universe_id"],
            resident_id=scope["resident_id"],
            scope="resident",
            category="companion_followup",
            source_type="commitment",
            source_id=f"concurrent-{index}",
            idempotency_key=f"resident-obligation:v1:commitment:concurrent-{index}",
            request_fingerprint=f"fingerprint-{index}",
            title=None,
            body_text=f"并发通知 {index}",
            target_type="none",
            target_id=None,
            delivered_at=f"2026-07-22 12:00:0{index}",
            expires_at="2026-08-21 12:00:00",
            now="2026-07-22 12:00:10",
            max_visible=1,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(_insert, range(2)))

    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM app_notifications "
            "WHERE platform_user_id=? AND delivery_status='visible'",
            (user_id,),
        ).fetchone()["c"]
    assert int(count) == 1
