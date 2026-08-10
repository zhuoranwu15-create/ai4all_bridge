"""v1.5 异步许愿：受理、持久 worker、时间窗、信箱关联与闭环。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

import app.db as db
from app.products.mingchan.application.resident_wishes import (
    CompanionWorldResidentWishService,
)


def _verified_token(phone: str) -> str:
    row = db.create_phone_verification(phone=phone, code="999999", expires_minutes=10)
    return db.set_verification_verified(row["id"], token_expires_minutes=10)[
        "verified_token"
    ]


def _login(client, phone: str) -> dict:
    response = client.post(
        "/api/v1/products/mingchan/auth/session",
        json={"phone": phone, "verified_token": _verified_token(phone)},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _seed_catalog() -> None:
    for rank in range(1, 5):
        db.create_character_template(
            template_id=f"tmpl_wish_{rank}",
            source_type="operations",
            name=f"角色{rank}",
            avatar_ref=f"asset://avatar-{rank}",
            summary=f"简介{rank}",
            tags_json=json.dumps(["温柔", "好奇", f"类型{rank}"], ensure_ascii=False),
            persona_seed_json=json.dumps(
                {
                    "SOUL.md": f"# SOUL\n\n人格{rank}",
                    "IDENTITY.md": f"# IDENTITY\n\n- 你的名字是角色{rank}",
                },
                ensure_ascii=False,
            ),
            persona_version=f"v{rank}",
            initial_candidate_rank=rank,
        )


def _bootstrapped(client, phone: str) -> dict:
    with db.connect() as conn:
        seeded = conn.execute(
            "SELECT COUNT(*) c FROM character_templates WHERE source_type='operations'"
        ).fetchone()["c"]
    if not seeded:
        _seed_catalog()
    headers = _login(client, phone)
    assert client.post("/api/v1/products/mingchan/worlds/home/bootstrap", headers=headers).status_code == 200
    # 本组只测许愿，不重复验证 confirm 的欢迎语写入；直接把已 bootstrap 的 world
    # 推到正式链路要求的 confirmed 前置状态。
    with db.connect() as conn:
        conn.execute(
            "UPDATE universes SET onboarding_state = 'confirmed' "
            "WHERE owner_platform_user_id = (SELECT id FROM platform_users WHERE phone = ?)",
            (phone,),
        )
    return headers


def _enabled(fresh_db) -> None:
    fresh_db.mingchan_p1_enabled = True
    fresh_db.mingchan_mailbox_enabled = True
    fresh_db.companion_world_wish_daily_max = 10


def _stub_input_review(monkeypatch, *, verdict: str = "pass") -> None:
    monkeypatch.setattr(
        "app.platform.moderation.text_sanitizer.generate_completion",
        lambda *_args, **_kwargs: json.dumps(
            {
                "verdict": verdict,
                "sanitized_text": "我想遇见一位愿意听我慢慢说的朋友",
                "categories": ["deceased_memorial"] if verdict == "reject" else [],
                "reason": "test",
            },
            ensure_ascii=False,
        ),
    )


_GOOD = {
    "name": "阿岚",
    "relationship_type": "friend",
    "personality_traits": ["gentle", "humorous"],
    "avatar_key": "atang",
    "style_note": "说话慢一点，喜欢先听我说完",
}


def _stub_generation_and_review(monkeypatch, *, review: str = "pass") -> None:
    def _generate(messages, **_kwargs):
        system = messages[0]["content"]
        if "final safety and quality reviewer" in system:
            return json.dumps(
                {"verdict": review, "categories": [], "reason": "test"},
                ensure_ascii=False,
            )
        return json.dumps(_GOOD, ensure_ascii=False)

    monkeypatch.setattr(
        "app.products.mingchan.application.wish.generate_completion",
        _generate,
    )


def _submit(client, headers, request_id: str, text: str = "我想要一个朋友"):
    return client.post(
        "/api/v1/products/mingchan/worlds/home/resident-wishes",
        headers=headers,
        json={"wish_text": text, "client_request_id": request_id},
    )


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=None)


def test_mailbox_gate_and_old_preview_wish_branch_is_closed(
    client, fresh_db, monkeypatch
):
    fresh_db.mingchan_p1_enabled = True
    fresh_db.mingchan_mailbox_enabled = False
    headers = _bootstrapped(client, "19930005001")

    hidden = _submit(client, headers, "wish-off-0001")
    legacy = client.post(
        "/api/v1/products/mingchan/worlds/home/resident-drafts/preview",
        headers=headers,
        json={"wish_text": "我想要一个朋友", "client_request_id": "wish-old-0001"},
    )

    assert hidden.status_code == 404
    assert hidden.json()["code"] == "feature_disabled"
    assert legacy.status_code == 422
    assert legacy.json()["code"] == "invalid_request"


def test_submit_is_202_durable_and_idempotent(client, fresh_db, monkeypatch):
    _enabled(fresh_db)
    headers = _bootstrapped(client, "19930005002")
    _stub_input_review(monkeypatch)

    first = _submit(client, headers, "wish-submit-0001")
    replay = _submit(client, headers, "wish-submit-0001")

    assert first.status_code == replay.status_code == 202
    first_data = first.json()["data"]
    replay_data = replay.json()["data"]
    assert first_data["replayed"] is False
    assert replay_data["replayed"] is True
    assert replay_data["wish"] == first_data["wish"]
    wish = first_data["wish"]
    assert wish["status"] == "pending"
    assert wish["is_open"] is True and wish["can_withdraw"] is True
    assert wish["letter_id"] is None
    assert _parse(wish["expected_delivery_from"]) - _parse(wish["submitted_at"]) == timedelta(
        hours=24
    )
    assert _parse(wish["expected_delivery_to"]) - _parse(wish["submitted_at"]) == timedelta(
        hours=72
    )
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM resident_wishes").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM resident_wish_jobs").fetchone()["c"] == 1


def test_idempotency_conflict_pending_guard_and_owner_isolation(
    client, fresh_db, monkeypatch
):
    _enabled(fresh_db)
    mine = _bootstrapped(client, "19930005003")
    theirs = _bootstrapped(client, "19930005004")
    _stub_input_review(monkeypatch)
    first = _submit(client, mine, "wish-shared-0001")

    conflict = _submit(client, mine, "wish-shared-0001", "不同愿望")
    pending = _submit(client, mine, "wish-other-0001")
    other = _submit(client, theirs, "wish-shared-0001")

    assert first.status_code == other.status_code == 202
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
    assert pending.status_code == 409 and pending.json()["code"] == "wish_already_pending"
    assert first.json()["data"]["wish"]["wish_id"] != other.json()["data"]["wish"]["wish_id"]


def test_input_review_rejects_before_wish_or_job_is_written(
    client, fresh_db, monkeypatch
):
    _enabled(fresh_db)
    headers = _bootstrapped(client, "19930005005")
    _stub_input_review(monkeypatch, verdict="reject")

    response = _submit(client, headers, "wish-reject-0001")

    assert response.status_code == 422
    assert response.json()["code"] == "wish_text_rejected"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM resident_wishes").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM resident_wish_jobs").fetchone()["c"] == 0


def test_current_and_withdraw_are_owner_scoped_and_idempotent(
    client, fresh_db, monkeypatch
):
    _enabled(fresh_db)
    mine = _bootstrapped(client, "19930005006")
    theirs = _bootstrapped(client, "19930005007")
    _stub_input_review(monkeypatch)
    wish = _submit(client, mine, "wish-withdraw-0001").json()["data"]["wish"]

    current = client.get("/api/v1/products/mingchan/worlds/home/resident-wishes/current", headers=mine)
    hidden = client.post(
        f"/api/v1/products/mingchan/resident-wishes/{wish['wish_id']}/withdraw", headers=theirs, json={}
    )
    first = client.post(
        f"/api/v1/products/mingchan/resident-wishes/{wish['wish_id']}/withdraw", headers=mine, json={}
    )
    replay = client.post(
        f"/api/v1/products/mingchan/resident-wishes/{wish['wish_id']}/withdraw", headers=mine, json={}
    )

    assert current.json()["data"]["wish"] == wish
    assert hidden.status_code == 404 and hidden.json()["code"] == "wish_not_found"
    assert first.json()["data"]["wish"]["status"] == "withdrawn"
    assert first.json()["data"]["replayed"] is False
    assert replay.json()["data"]["replayed"] is True
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM resident_wish_jobs").fetchone()
        assert row["status"] == "cancelled"
        assert conn.execute("SELECT wish_text FROM resident_wishes").fetchone()["wish_text"] is None


def test_worker_generates_then_delivers_wish_letter_exactly_once(
    client, fresh_db, monkeypatch
):
    _enabled(fresh_db)
    fresh_db.mingchan_mailbox_enabled = True
    headers = _bootstrapped(client, "19930005008")
    _stub_input_review(monkeypatch)
    _stub_generation_and_review(monkeypatch)
    wish = _submit(client, headers, "wish-worker-0001").json()["data"]["wish"]
    submitted = _parse(wish["submitted_at"])
    service = CompanionWorldResidentWishService(config=fresh_db)

    generated = service.maintain_batch(now=submitted + timedelta(minutes=1), batch_size=10)
    early = service.maintain_batch(now=submitted + timedelta(hours=23), batch_size=10)
    delivered = service.maintain_batch(now=submitted + timedelta(hours=24), batch_size=10)
    replay = service.maintain_batch(now=submitted + timedelta(hours=25), batch_size=10)

    assert generated["metrics"]["generated"] == 1
    assert early["metrics"]["claimed"] == 0
    assert delivered["metrics"]["delivered"] == 1
    assert replay["metrics"]["claimed"] == 0
    current = client.get(
        "/api/v1/products/mingchan/worlds/home/resident-wishes/current", headers=headers
    ).json()["data"]["wish"]
    assert current["status"] == "delivered"
    assert current["letter_id"] and current["is_open"] is True
    assert current["can_withdraw"] is False

    letter = client.get(
        f"/api/v1/products/mingchan/mailbox/letters/{current['letter_id']}", headers=headers
    ).json()["data"]["letter"]
    assert letter["source"] == "wish"
    assert letter["wish_id"] == wish["wish_id"]
    assert "我想" not in letter["body"]["text"]
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM character_letters WHERE wish_id = ?", (wish["wish_id"],)
        ).fetchone()["c"] == 1
        assert conn.execute("SELECT wish_text FROM resident_wishes").fetchone()["wish_text"] is None


def test_wish_letter_accept_closes_wish_and_creates_resident(
    client, fresh_db, monkeypatch
):
    _enabled(fresh_db)
    fresh_db.mingchan_mailbox_enabled = True
    headers = _bootstrapped(client, "19930005009")
    _stub_input_review(monkeypatch)
    _stub_generation_and_review(monkeypatch)
    wish = _submit(client, headers, "wish-accept-0001").json()["data"]["wish"]
    submitted = _parse(wish["submitted_at"])
    service = CompanionWorldResidentWishService(config=fresh_db)
    service.maintain_batch(now=submitted + timedelta(minutes=1), batch_size=10)
    service.maintain_batch(now=submitted + timedelta(hours=24), batch_size=10)
    current = client.get(
        "/api/v1/products/mingchan/worlds/home/resident-wishes/current", headers=headers
    ).json()["data"]["wish"]

    accepted = client.post(
        f"/api/v1/products/mingchan/mailbox/letters/{current['letter_id']}/accept", headers=headers, json={}
    )
    after = client.get(
        "/api/v1/products/mingchan/worlds/home/resident-wishes/current", headers=headers
    ).json()["data"]["wish"]

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["resident"]["status"] == "active"
    assert after["status"] == "delivered"
    assert after["is_open"] is False
    assert after["terminal_reason"] == "letter_accepted"


def test_failed_second_review_retries_then_becomes_unfulfilled(
    client, fresh_db, monkeypatch
):
    _enabled(fresh_db)
    fresh_db.companion_world_wish_retry_seconds = 3600
    headers = _bootstrapped(client, "19930005010")
    _stub_input_review(monkeypatch)
    _stub_generation_and_review(monkeypatch, review="block")
    wish = _submit(client, headers, "wish-unfulfilled-0001").json()["data"]["wish"]
    submitted = _parse(wish["submitted_at"])
    service = CompanionWorldResidentWishService(config=fresh_db)

    retried = service.maintain_batch(now=submitted + timedelta(minutes=1), batch_size=1)
    terminal = service.maintain_batch(now=submitted + timedelta(hours=72), batch_size=1)
    current = client.get(
        "/api/v1/products/mingchan/worlds/home/resident-wishes/current", headers=headers
    ).json()["data"]["wish"]

    assert retried["metrics"]["retried"] == 1
    assert terminal["metrics"]["unfulfilled"] == 1
    assert current["status"] == "unfulfilled"
    assert current["is_open"] is False
    assert current["letter_id"] is None


def test_app_config_publishes_async_wish_contract(client, fresh_db):
    fresh_db.mingchan_p1_enabled = True
    fresh_db.mingchan_mailbox_enabled = False
    body = client.get("/api/v1/products/mingchan/app/config").json()

    assert body["features"]["resident_wish_create"] is False
    fresh_db.mingchan_mailbox_enabled = True
    assert client.get("/api/v1/products/mingchan/app/config").json()["features"]["resident_wish_create"] is True
    assert body["limits"]["wish_text_chars"] == 500
    assert body["client_contract_version"] == "2026-08-04"
@pytest.fixture
def client(mingchan_client):
    return mingchan_client
