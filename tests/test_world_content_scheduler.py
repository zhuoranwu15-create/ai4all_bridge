"""M3-3 AI Feed 窗口、eligibility、状态机与 outbox worker 功能测试。"""
import asyncio
from datetime import datetime

import pytest

import app.db as db
from app.domains.companion_world import user_post_fingerprint
from app.world_content import (
    FeedWindows,
    WorldContentScheduler,
    dispatch_world_outbox_batch,
    generate_ai_feed_batch,
)
from app.world_content.scheduler import _run_metrics
from tests.factories import make_resident_account


class _StaticGenerator:
    def __init__(self, text: str = "窗外的光慢慢落下来，今天也有一点值得记住。") -> None:
        self.text = text
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return self.text


class _FailingGenerator:
    def generate(self, request):
        raise RuntimeError("forced generation failure")


class _RecordingPublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.keys = []

    def publish(self, *, event_type, payload, idempotency_key):
        if self.fail:
            raise RuntimeError("forced publisher failure")
        self.keys.append(idempotency_key)


def _windows() -> FeedWindows:
    return FeedWindows.parse(
        morning_start="09:00",
        morning_end="11:00",
        evening_start="18:00",
        evening_end="21:00",
    )


def _world_with_inbound(phone: str, name: str, *, inbound_at: str = "2026-07-22 08:00:00"):
    user_id = db.create_or_get_platform_user_by_phone(
        phone=phone, display_name=name
    )["id"]
    account_id = make_resident_account(user_id, name)
    scope = db.resolve_resident_memory_scope(runtime_account_id=account_id)
    db.set_universe_onboarding_state(
        universe_id=scope["universe_id"], onboarding_state="confirmed"
    )
    session = db.get_or_create_session(
        account_id=account_id,
        channel="native",
        sender_id="user",
        sender_name=None,
        chat_id=None,
        session_key=db.APP_ACTIVE_SESSION_KEY,
        business_day="2026-07-22",
    )["session"]
    message_id = db.insert_message(
        account_id=account_id,
        session_id=int(session["id"]),
        message_id=f"inbound-{phone}",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="早上好",
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE messages SET created_at = ? WHERE id = ?",
            (inbound_at, message_id),
        )
    return user_id, account_id, scope


def _run_generation(now: datetime, generator, *, max_attempts: int = 3, retry_base: int = 10):
    return generate_ai_feed_batch(
        now=now,
        windows=_windows(),
        generator=generator,
        batch_size=20,
        claim_lease_seconds=1,
        retry_max_attempts=max_attempts,
        retry_base_seconds=retry_base,
    )


def test_feed_windows_are_half_open_ordered_and_beijing_day_scoped():
    windows = _windows()
    assert windows.resolve(datetime(2026, 7, 22, 8, 59)) is None
    assert windows.resolve(datetime(2026, 7, 22, 9, 0)).name == "morning"
    assert windows.resolve(datetime(2026, 7, 22, 10, 59)).window_end_at == datetime(
        2026, 7, 22, 11, 0
    )
    assert windows.resolve(datetime(2026, 7, 22, 11, 0)) is None
    assert windows.resolve(datetime(2026, 7, 22, 18, 0)).name == "evening"
    with pytest.raises(ValueError):
        FeedWindows.parse(
            morning_start="10:00",
            morning_end="12:00",
            evening_start="11:00",
            evening_end="20:00",
        )
    with pytest.raises(ValueError):
        FeedWindows.parse(
            morning_start="",
            morning_end="11:00",
            evening_start="18:00",
            evening_end="21:00",
        )


def test_eligibility_and_author_selection_use_inbound_and_app_activity(fresh_db):
    _user, account_a, scope_a = _world_with_inbound("19963001001", "甲")
    account_b = make_resident_account(_user, "乙")
    scope_b = db.resolve_resident_memory_scope(runtime_account_id=account_b)
    session_b = db.get_or_create_session(
        account_id=account_b,
        channel="native",
        sender_id="user",
        sender_name=None,
        chat_id=None,
        session_key=db.APP_ACTIVE_SESSION_KEY,
        business_day="2026-07-22",
    )["session"]
    message_b = db.insert_message(
        account_id=account_b,
        session_id=int(session_b["id"]),
        message_id="author-activity-b",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="乙的 App 会话更近",
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE messages SET created_at='2026-07-22 08:30:00' WHERE id=?",
            (message_b,),
        )

    eligible = db.list_ai_feed_eligible_worlds(
        inbound_since="2026-07-15 09:30:00", limit=20
    )
    assert [row["universe_id"] for row in eligible] == [scope_a["universe_id"]]
    author = db.select_ai_feed_author(universe_id=scope_a["universe_id"])
    assert author["resident_id"] == scope_b["resident_id"]
    with db.connect() as conn:
        conn.execute(
            "UPDATE universe_residents SET status='offline' WHERE id=?",
            (scope_b["resident_id"],),
        )
    assert db.select_ai_feed_author(
        universe_id=scope_a["universe_id"]
    )["resident_id"] == scope_a["resident_id"]


def test_ai_feed_generates_once_per_slot_and_publishes_outbox(fresh_db):
    _user, _account, scope = _world_with_inbound("19963001002", "生成居民")
    generator = _StaticGenerator()
    first = _run_generation(datetime(2026, 7, 22, 9, 30), generator)
    replay = _run_generation(datetime(2026, 7, 22, 9, 31), generator)
    assert first["published"] == 1
    assert replay["published"] == 0
    assert replay["results"][0]["status"] == "already_claimed"
    assert len(generator.requests) == 1
    with db.connect() as conn:
        post = conn.execute(
            "SELECT * FROM universe_posts WHERE universe_id = ?",
            (scope["universe_id"],),
        ).fetchone()
        assert post["status"] == "published" and post["ai_slot"] == "morning"
        assert post["attempt_count"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM companion_world_outbox WHERE post_id=?",
            (post["id"],),
        ).fetchone()["c"] == 1


def test_ai_feed_batch_cursor_advances_past_already_processed_worlds(fresh_db):
    _world_with_inbound("19963001007", "分页居民甲")
    _world_with_inbound("19963001008", "分页居民乙")
    generator = _StaticGenerator()
    first = generate_ai_feed_batch(
        now=datetime(2026, 7, 22, 9, 30),
        windows=_windows(),
        generator=generator,
        batch_size=1,
        claim_lease_seconds=1,
        retry_max_attempts=3,
        retry_base_seconds=10,
    )
    second = generate_ai_feed_batch(
        now=datetime(2026, 7, 22, 9, 31),
        windows=_windows(),
        generator=generator,
        batch_size=1,
        claim_lease_seconds=1,
        retry_max_attempts=3,
        retry_base_seconds=10,
        after_universe_id=first["next_after_universe_id"],
    )
    assert first["published"] == second["published"] == 1
    assert len(generator.requests) == 2
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM universe_posts WHERE status='published'"
        ).fetchone()["c"] == 2


def test_scheduler_resets_generation_cursor_for_each_slot(fresh_db):
    _world_with_inbound("19963001009", "换窗居民甲")
    _world_with_inbound("19963001010", "换窗居民乙")
    generator = _StaticGenerator()
    scheduler = WorldContentScheduler(
        enabled=True,
        windows=_windows(),
        interval_seconds=60,
        batch_size=1,
        claim_lease_seconds=1,
        retry_max_attempts=3,
        retry_base_seconds=10,
        outbox_batch_size=10,
        outbox_claim_lease_seconds=10,
        outbox_max_attempts=3,
        generator=generator,
        publisher=_RecordingPublisher(),
    )
    morning = asyncio.run(scheduler.run_once(now=datetime(2026, 7, 22, 9, 30)))
    scheduler._heartbeat("ok")
    heartbeat = db.get_scheduler_heartbeat("world_content_scheduler")
    evening = asyncio.run(scheduler.run_once(now=datetime(2026, 7, 22, 18, 30)))
    assert morning["generation"]["published"] == 1
    assert evening["generation"]["published"] == 1
    assert morning["metrics"] == {
        "feed_slot_claimed": 1,
        "feed_slot_claim_conflict": 0,
        "feed_slot_skipped": 0,
        "feed_retry_scheduled": 0,
        "feed_retry_exhausted": 0,
        "feed_published": 1,
        "outbox_claimed": 1,
        "outbox_delivered": 1,
        "outbox_failed": 0,
        "outbox_dead": 0,
        "outbox_pending": 0,
        "outbox_processing": 0,
        "outbox_lag_seconds": 0,
    }
    assert heartbeat["metadata"]["last_run_metrics"] == morning["metrics"]
    assert generator.requests[0].universe_id == generator.requests[1].universe_id
    assert generator.requests[0].slot == "morning"
    assert generator.requests[1].slot == "evening"


def test_generation_retries_then_skips_at_attempt_limit(fresh_db):
    _world_with_inbound("19963001003", "重试居民")
    generator = _FailingGenerator()
    first = _run_generation(datetime(2026, 7, 22, 9, 0, 0), generator)
    second = _run_generation(datetime(2026, 7, 22, 9, 0, 11), generator)
    third = _run_generation(datetime(2026, 7, 22, 9, 0, 32), generator)
    assert first["results"][0]["status"] == "generation_failed"
    assert second["results"][0]["status"] == "generation_failed"
    assert third["results"][0]["status"] == "generation_failed"
    assert first["results"][0]["retry_status"] == "generating"
    assert third["results"][0]["retry_status"] == "skipped"
    metrics = _run_metrics(
        third,
        {
            "claimed": 0,
            "delivered": 0,
            "failed": 0,
            "queue": {},
        },
    )
    assert metrics["feed_retry_exhausted"] == 1
    assert metrics["feed_slot_skipped"] == 1
    with db.connect() as conn:
        post = conn.execute("SELECT * FROM universe_posts").fetchone()
    assert post["attempt_count"] == 3
    assert post["status"] == "skipped"
    assert post["terminal_reason"] == "retry_exhausted"


def test_window_close_marks_generating_slot_skipped(fresh_db):
    _user, _account, scope = _world_with_inbound("19963001006", "关窗居民")
    author = db.select_ai_feed_author(universe_id=scope["universe_id"])
    post, created = db.claim_ai_feed_slot(
        universe_id=scope["universe_id"],
        author_resident_id=author["resident_id"],
        ai_local_date="2026-07-22",
        ai_slot="morning",
        slot_window_end_at="2026-07-22 11:00:00",
        claim_token="window-close-test",
        claimed_at="2026-07-22 10:59:00",
    )
    assert created is True
    result = _run_generation(datetime(2026, 7, 22, 11, 0), _StaticGenerator())
    assert result["status"] == "outside_window" and result["closed_expired"] == 1
    with db.connect() as conn:
        current = conn.execute(
            "SELECT * FROM universe_posts WHERE id=?", (post["id"],)
        ).fetchone()
    assert current["status"] == "skipped"
    assert current["terminal_reason"] == "window_closed"


def test_author_inactive_during_generation_skips_without_relabel(fresh_db):
    _user, _account, scope = _world_with_inbound("19963001004", "离场居民")

    class _OffliningGenerator:
        def generate(self, request):
            with db.connect() as conn:
                conn.execute(
                    "UPDATE universe_residents SET status='offline' WHERE id=?",
                    (request.resident_id,),
                )
            return "这条不应发布"

    result = _run_generation(
        datetime(2026, 7, 22, 9, 30), _OffliningGenerator()
    )
    assert result["published"] == 0
    with db.connect() as conn:
        post = conn.execute(
            "SELECT * FROM universe_posts WHERE universe_id=?",
            (scope["universe_id"],),
        ).fetchone()
    assert post["status"] == "skipped"
    assert post["terminal_reason"] == "author_inactive"
    assert post["text"] is None


def test_outbox_worker_delivers_and_retries_to_dead(fresh_db):
    user_id, _account, _scope = _world_with_inbound("19963001005", "事件居民")
    text = "用户动态事件"
    post, _ = db.publish_user_feed_post_with_outbox(
        owner_platform_user_id=user_id,
        client_request_id="world_outbox_test_1",
        text=text,
        request_fingerprint=user_post_fingerprint(text),
        published_at="2026-07-22 10:00:00",
    )
    publisher = _RecordingPublisher()
    delivered = dispatch_world_outbox_batch(
        now=datetime(2026, 7, 22, 10, 0, 1),
        publisher=publisher,
        batch_size=10,
        claim_lease_seconds=10,
        max_attempts=2,
        retry_base_seconds=10,
    )
    assert delivered["delivered"] == 1
    assert publisher.keys == [f"world-post-published:v1:{post['id']}"]

    text2 = "失败事件"
    post2, _ = db.publish_user_feed_post_with_outbox(
        owner_platform_user_id=user_id,
        client_request_id="world_outbox_test_2",
        text=text2,
        request_fingerprint=user_post_fingerprint(text2),
        published_at="2026-07-22 10:01:00",
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE companion_world_outbox SET created_at='2026-07-22 10:01:00' "
            "WHERE post_id=?",
            (post2["id"],),
        )
    failing = _RecordingPublisher(fail=True)
    first_fail = dispatch_world_outbox_batch(
        now=datetime(2026, 7, 22, 10, 1, 1),
        publisher=failing,
        batch_size=10,
        claim_lease_seconds=10,
        max_attempts=2,
        retry_base_seconds=10,
    )
    second_fail = dispatch_world_outbox_batch(
        now=datetime(2026, 7, 22, 10, 1, 12),
        publisher=failing,
        batch_size=10,
        claim_lease_seconds=10,
        max_attempts=2,
        retry_base_seconds=10,
    )
    assert first_fail["failed"] == 1 and first_fail["dead"] == 0
    assert second_fail["failed"] == 1 and second_fail["dead"] == 1
    assert first_fail["queue"]["pending"] == 1
    assert first_fail["queue"]["oldest_undelivered_lag_seconds"] == 1
    assert second_fail["queue"]["dead"] == 1
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM companion_world_outbox WHERE post_id=?",
            (post2["id"],),
        ).fetchone()
    assert row["status"] == "dead" and row["attempts"] == 2
