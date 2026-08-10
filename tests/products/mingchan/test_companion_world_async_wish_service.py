"""异步许愿应用服务的无 HTTP 聚焦测试；用于双后端状态机与事务门禁。"""
from __future__ import annotations

import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import app.db as db
import pytest
from app.bootstrap.product_registry import MINGCHAN_APP_ID, build_test_product_registry
from app.products.mingchan.application.mailbox import (
    CompanionWorldMailboxService,
)
from app.products.mingchan.application.account_deletion import execute_account_deletion
from app.products.mingchan.application.resident_wishes import (
    CompanionWorldResidentWishService,
    ResidentWishError,
)
from app.products.mingchan.infrastructure.persistence.resident_wishes import (
    claim_resident_wish_job,
    complete_resident_wish_generation,
)

NOW = datetime(2026, 7, 31, 20, 0, 0)
TEST_REGISTRY = build_test_product_registry()


def _world(phone: str) -> tuple[str, dict]:
    owner = db.create_or_get_platform_user_by_phone(phone=phone, display_name="用户")["id"]
    db.ensure_product_membership(
        platform_user_id=owner,
        app_id=MINGCHAN_APP_ID,
        registry=TEST_REGISTRY,
    )
    world = db.get_or_create_home_universe(platform_user_id=owner)
    with db.connect() as conn:
        conn.execute(
            "UPDATE universes SET onboarding_state='confirmed' WHERE id=?", (world["id"],)
        )
    return owner, {**world, "onboarding_state": "confirmed"}


def _stub_input(monkeypatch, *, verdict: str = "pass") -> None:
    monkeypatch.setattr(
        "app.platform.moderation.text_sanitizer.generate_completion",
        lambda *_args, **_kwargs: json.dumps(
            {
                "verdict": verdict,
                "sanitized_text": "我想遇见一个愿意认真听我说话的朋友",
                "categories": ["minor_persona"] if verdict == "reject" else [],
            },
            ensure_ascii=False,
        ),
    )


def _stub_worker(monkeypatch, *, review: str = "pass") -> None:
    def _generate(messages, **_kwargs):
        if "final safety and quality reviewer" in messages[0]["content"]:
            return json.dumps({"verdict": review, "categories": []})
        return json.dumps(
            {
                "name": "阿岚",
                "relationship_type": "friend",
                "personality_traits": ["gentle", "humorous"],
                "avatar_key": "atang",
                "style_note": "先听完，再诚实回应",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        "app.products.mingchan.application.wish.generate_completion",
        _generate,
    )


def test_submit_replay_conflict_and_withdraw(fresh_db, monkeypatch):
    owner, _ = _world("19930101001")
    _stub_input(monkeypatch)
    service = CompanionWorldResidentWishService(config=fresh_db)

    first, replayed = service.submit(
        owner,
        wish_text="我想要一个朋友",
        client_request_id="wish-service-0001",
        now=NOW,
    )
    replay, replayed_again = service.submit(
        owner,
        wish_text="我想要一个朋友",
        client_request_id="wish-service-0001",
        now=NOW + timedelta(minutes=1),
    )

    assert replayed is False and replayed_again is True
    assert replay == first
    assert first.expected_delivery_from == "2026-08-01 20:00:00"
    assert first.expected_delivery_to == "2026-08-03 20:00:00"
    try:
        service.submit(
            owner,
            wish_text="换一个愿望",
            client_request_id="wish-service-0001",
            now=NOW,
        )
    except ResidentWishError as err:
        assert err.code == "idempotency_conflict"
    else:
        raise AssertionError("different payload reused the same idempotency key")

    withdrawn, withdraw_replayed = service.withdraw(
        owner, wish_id=first.wish_id, now=NOW + timedelta(hours=1)
    )
    withdrawn_again, withdraw_replayed_again = service.withdraw(
        owner, wish_id=first.wish_id, now=NOW + timedelta(hours=2)
    )
    assert withdraw_replayed is False and withdraw_replayed_again is True
    assert withdrawn.status == withdrawn_again.status == "withdrawn"
    assert withdrawn.is_open is False
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM resident_wish_jobs").fetchone()["status"] == "cancelled"


def test_worker_delivers_once_and_accept_closes_wish(fresh_db, monkeypatch):
    owner, _ = _world("19930101002")
    _stub_input(monkeypatch)
    _stub_worker(monkeypatch)
    service = CompanionWorldResidentWishService(config=fresh_db)
    wish, _ = service.submit(
        owner,
        wish_text="我想要一个朋友",
        client_request_id="wish-deliver-0001",
        now=NOW,
    )

    generated = service.maintain_batch(now=NOW + timedelta(minutes=1), batch_size=10)
    early = service.maintain_batch(now=NOW + timedelta(hours=23), batch_size=10)
    delivered = service.maintain_batch(now=NOW + timedelta(hours=24), batch_size=10)
    replay = service.maintain_batch(now=NOW + timedelta(hours=25), batch_size=10)

    assert generated["metrics"]["generated"] == 1
    assert early["metrics"]["claimed"] == 0
    assert delivered["metrics"]["delivered"] == 1
    assert replay["metrics"]["claimed"] == 0
    current = service.current(owner, now=NOW + timedelta(hours=24))
    assert current is not None and current.status == "delivered"
    assert current.letter_id and current.is_open
    with db.connect() as conn:
        letter = dict(
            conn.execute(
                "SELECT * FROM character_letters WHERE wish_id=?", (wish.wish_id,)
            ).fetchone()
        )
        assert letter["source"] == "wish"
        assert conn.execute(
            "SELECT COUNT(*) c FROM character_letters WHERE wish_id=?", (wish.wish_id,)
        ).fetchone()["c"] == 1

    accepted = CompanionWorldMailboxService(registry=TEST_REGISTRY).accept_letter(
        owner,
        letter_id=current.letter_id,
        now="2026-08-01 20:01:00",
    )
    assert accepted["resident"]["status"] == "active"
    closed = service.current(owner, now=NOW + timedelta(hours=25))
    assert closed is not None and closed.is_open is False
    assert closed.terminal_reason == "letter_accepted"


def test_second_review_failure_retries_then_unfulfilled(fresh_db, monkeypatch):
    owner, _ = _world("19930101003")
    _stub_input(monkeypatch)
    _stub_worker(monkeypatch, review="block")
    service = CompanionWorldResidentWishService(config=fresh_db)
    wish, _ = service.submit(
        owner,
        wish_text="我想要一个朋友",
        client_request_id="wish-retry-0001",
        now=NOW,
    )

    retry = service.maintain_batch(now=NOW + timedelta(minutes=1), batch_size=1)
    terminal = service.maintain_batch(now=NOW + timedelta(hours=72), batch_size=1)

    assert retry["metrics"]["retried"] == 1
    assert terminal["metrics"]["unfulfilled"] == 1
    current = service.current(owner, now=NOW + timedelta(hours=72))
    assert current is not None and current.status == "unfulfilled"
    assert current.is_open is False and current.letter_id is None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM resident_wishes WHERE id=?", (wish.wish_id,)).fetchone()
        assert row["wish_text"] is None
        assert conn.execute("SELECT COUNT(*) c FROM character_letters").fetchone()["c"] == 0


def test_cross_owner_wish_id_is_not_visible_or_withdrawable(fresh_db, monkeypatch):
    mine, _ = _world("19930101004")
    theirs, _ = _world("19930101005")
    _stub_input(monkeypatch)
    service = CompanionWorldResidentWishService(config=fresh_db)
    wish, _ = service.submit(
        mine,
        wish_text="我想要一个朋友",
        client_request_id="wish-owner-0001",
        now=NOW,
    )

    try:
        service.withdraw(theirs, wish_id=wish.wish_id, now=NOW)
    except ResidentWishError as err:
        assert err.code == "wish_not_found"
    else:
        raise AssertionError("cross-owner withdraw must be hidden")


def test_worker_rechecks_account_before_generation(fresh_db, monkeypatch):
    owner, _ = _world("19930101007")
    _stub_input(monkeypatch)
    service = CompanionWorldResidentWishService(config=fresh_db)
    service.submit(
        owner,
        wish_text="我想要一个朋友",
        client_request_id="wish-disabled-0001",
        now=NOW,
    )
    with db.connect() as conn:
        conn.execute("UPDATE platform_users SET status='deactivated' WHERE id=?", (owner,))
    monkeypatch.setattr(
        "app.products.mingchan.application.wish.generate_completion",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled account must not generate")
        ),
    )

    result = service.maintain_batch(now=NOW + timedelta(minutes=1), batch_size=1)
    current = service.current(owner, now=NOW + timedelta(minutes=1))

    assert result["metrics"]["unfulfilled"] == 1
    assert current is not None and current.status == "unfulfilled"
    assert current.terminal_reason == "account_unavailable"


def test_account_deletion_removes_pending_wish_and_job(fresh_db, monkeypatch):
    owner, _ = _world("19930101008")
    _stub_input(monkeypatch)
    CompanionWorldResidentWishService(config=fresh_db).submit(
        owner,
        wish_text="我想要一个朋友",
        client_request_id="wish-delete-0001",
        now=NOW,
    )

    stats = execute_account_deletion(
        platform_user_id=owner,
        now=NOW,
        registry=TEST_REGISTRY,
    )

    assert stats["resident_wishes_deleted"] == 1
    assert stats["resident_wish_jobs_deleted"] == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM resident_wishes").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM resident_wish_jobs").fetchone()["c"] == 0


def test_withdraw_and_delivery_race_has_one_atomic_winner(fresh_db, monkeypatch):
    owner, _ = _world("19930101006")
    _stub_input(monkeypatch)
    service = CompanionWorldResidentWishService(config=fresh_db)
    wish, _ = service.submit(
        owner,
        wish_text="我想要一个朋友",
        client_request_id="wish-race-0001",
        now=NOW,
    )
    token = secrets.token_urlsafe(24)
    claimed = claim_resident_wish_job(
        now="2026-07-31 20:01:00",
        lease_expires_at="2026-07-31 20:11:00",
        claim_token=token,
    )
    assert claimed is not None
    generation = {
        "name": "阿岚",
        "avatar_ref": "/companion_world/avatars/atang.png",
        "relationship_type": "friend",
        "personality_traits": ["gentle"],
        "normalized_summary": "阿岚，你的朋友，温柔。",
        "tags": ["温柔"],
        "persona_seed": {
            "SOUL.md": "# SOUL\n\n你是 AI 居民阿岚。",
            "IDENTITY.md": "# IDENTITY\n\n- 你的名字是阿岚。",
        },
        "letter_body": "你好，我是阿岚。愿意的话，我们可以先认识一下。",
    }
    assert complete_resident_wish_generation(
        wish_id=wish.wish_id,
        claim_token=token,
        generation=generation,
        safety={"candidate_review": {"verdict": "pass"}},
        next_attempt_at="2026-08-01 20:00:00",
        now="2026-07-31 20:01:00",
    )

    barrier = threading.Barrier(2)

    def _deliver():
        barrier.wait()
        return service.maintain_batch(now=NOW + timedelta(hours=24), batch_size=1)

    def _withdraw():
        barrier.wait()
        try:
            return service.withdraw(owner, wish_id=wish.wish_id, now=NOW + timedelta(hours=24))
        except ResidentWishError as err:
            return err.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        delivery_result = pool.submit(_deliver)
        withdraw_result = pool.submit(_withdraw)
        delivery = delivery_result.result(timeout=10)
        withdrawal = withdraw_result.result(timeout=10)

    current = service.current(owner, now=NOW + timedelta(hours=24, minutes=1))
    assert current is not None
    with db.connect() as conn:
        letter_count = conn.execute(
            "SELECT COUNT(*) c FROM character_letters WHERE wish_id=?", (wish.wish_id,)
        ).fetchone()["c"]
    if current.status == "withdrawn":
        assert letter_count == 0
        assert not isinstance(withdrawal, str)
        assert delivery["metrics"]["delivered"] == 0
    else:
        assert current.status == "delivered"
        assert letter_count == 1
        assert withdrawal == "wish_not_withdrawable"
        assert delivery["metrics"]["delivered"] == 1
