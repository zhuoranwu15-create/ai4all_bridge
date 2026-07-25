"""M5-4 独立真人聊天、只读历史、self-hide、举报与 PG 发送竞态。"""
from __future__ import annotations

import concurrent.futures
import json
from datetime import datetime

import pytest

import app.db as db
from app.db._backend import is_postgres
from app.products.zhaoxi.application.companion_world_human_chat import (
    CompanionWorldHumanChatService,
    HumanChatError,
)
from app.products.zhaoxi.application.companion_world_visits import CompanionWorldVisitService

NOW = datetime(2026, 7, 23, 12, 0, 0)


def _login(client, phone: str) -> tuple[dict, dict]:
    verification = db.create_phone_verification(
        phone=phone, code="999999", expires_minutes=10
    )
    verified = db.set_verification_verified(
        verification["id"], token_expires_minutes=10
    )["verified_token"]
    response = client.post(
        "/v1/auth/session", json={"phone": phone, "verified_token": verified}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    return {"Authorization": f"Bearer {payload['access_token']}"}, payload


def _confirm_world(platform_user_id: str) -> dict:
    world = db.get_or_create_home_universe(platform_user_id=platform_user_id)
    db.set_universe_onboarding_state(
        universe_id=world["id"], onboarding_state="confirmed"
    )
    return db.get_universe(universe_id=world["id"])


def _user(phone: str) -> str:
    return db.create_or_get_platform_user_by_phone(phone=phone)["id"]


def _active(owner_id: str, visitor_id: str) -> tuple[dict, dict]:
    _confirm_world(owner_id)
    invitation = CompanionWorldVisitService().create_invite(owner_id, now=NOW)
    visit = CompanionWorldVisitService().redeem(
        visitor_id, code=invitation["code"], now=NOW
    )
    accepted = CompanionWorldVisitService().accept(
        owner_id, visit_id=visit["id"], now=NOW
    )
    return accepted["visit"], accepted["conversation"]


def _enable(monkeypatch, *, write: bool = True) -> None:
    monkeypatch.setattr(
        "app.products.zhaoxi.api.companion_world.settings.companion_world_p1_enabled", True
    )
    monkeypatch.setattr(
        "app.products.zhaoxi.api.companion_world_human_chat.settings.companion_world_human_chat_enabled",
        write,
    )
    monkeypatch.setattr(
        "app.products.zhaoxi.api.companion_world_human_chat.beijing_naive_now", lambda: NOW
    )


def test_human_chat_write_flag_off_keeps_history_readable(
    client, fresh_db, monkeypatch
):
    _enable(monkeypatch, write=False)
    owner_headers, owner_login = _login(client, "19965201001")
    visitor_headers, visitor_login = _login(client, "19965201002")
    owner = owner_login["platform_user"]["id"]
    visitor = visitor_login["platform_user"]["id"]
    _visit, conversation = _active(owner, visitor)

    listed = client.get("/v1/human-conversations", headers=owner_headers)
    assert listed.status_code == 200
    assert listed.json()["data"]["items"][0]["conversation_id"] == conversation["id"]
    blocked = client.post(
        f"/v1/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json={"client_message_id": "client_0001", "text": "hello"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "human_chat_read_only"
    history = client.get(
        f"/v1/human-conversations/{conversation['id']}/messages",
        headers=visitor_headers,
    )
    assert history.status_code == 200
    assert history.json()["data"]["items"] == []


def test_human_messages_are_idempotent_participant_scoped_and_not_ai_messages(
    client, fresh_db, monkeypatch
):
    _enable(monkeypatch)
    owner_headers, owner_login = _login(client, "19965202001")
    visitor_headers, visitor_login = _login(client, "19965202002")
    outsider_headers, _outsider_login = _login(client, "19965202003")
    owner = owner_login["platform_user"]["id"]
    visitor = visitor_login["platform_user"]["id"]
    _visit, conversation = _active(owner, visitor)
    with db.connect() as conn:
        ai_before = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]

    sent = client.post(
        f"/v1/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json={"client_message_id": "client_0001", "text": "  hello  "},
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["data"]["created"] is True
    message_id = sent.json()["data"]["message"]["message_id"]
    replay = client.post(
        f"/v1/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json={"client_message_id": "client_0001", "text": "hello"},
    )
    assert replay.status_code == 200
    assert replay.json()["data"]["created"] is False
    assert replay.json()["data"]["message"]["message_id"] == message_id
    conflict = client.post(
        f"/v1/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json={"client_message_id": "client_0001", "text": "changed"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"

    reply = client.post(
        f"/v1/human-conversations/{conversation['id']}/messages",
        headers=visitor_headers,
        json={"client_message_id": "client_0002", "text": "hi"},
    )
    assert reply.status_code == 200
    owner_history = client.get(
        f"/v1/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
    )
    assert [item["sender"] for item in owner_history.json()["data"]["items"]] == [
        "counterpart",
        "self",
    ]
    assert client.get(
        f"/v1/human-conversations/{conversation['id']}/messages",
        headers=outsider_headers,
    ).status_code == 404
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == ai_before
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM human_messages"
        ).fetchone()["n"] == 2


def test_visit_terminal_keeps_human_history_but_disables_new_send(
    client, fresh_db, monkeypatch
):
    _enable(monkeypatch)
    owner_headers, owner_login = _login(client, "19965203001")
    visitor_headers, visitor_login = _login(client, "19965203002")
    owner = owner_login["platform_user"]["id"]
    visitor = visitor_login["platform_user"]["id"]
    visit, conversation = _active(owner, visitor)
    assert client.post(
        f"/v1/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json={"client_message_id": "client_0001", "text": "留存消息"},
    ).status_code == 200
    CompanionWorldVisitService().terminate(
        visitor, visit_id=visit["id"], action="leave", now=NOW
    )

    denied = client.post(
        f"/v1/human-conversations/{conversation['id']}/messages",
        headers=visitor_headers,
        json={"client_message_id": "client_0002", "text": "不能再发"},
    )
    assert denied.status_code == 409
    assert denied.json()["code"] == "human_chat_read_only"
    history = client.get(
        f"/v1/human-conversations/{conversation['id']}/messages",
        headers=visitor_headers,
    )
    assert history.status_code == 200
    assert history.json()["data"]["items"][0]["content"]["text"] == "留存消息"


def test_self_hide_only_hides_current_participant_and_never_deletes_messages(
    client, fresh_db, monkeypatch
):
    _enable(monkeypatch)
    owner_headers, owner_login = _login(client, "19965204001")
    visitor_headers, visitor_login = _login(client, "19965204002")
    owner = owner_login["platform_user"]["id"]
    visitor = visitor_login["platform_user"]["id"]
    _visit, conversation = _active(owner, visitor)
    client.post(
        f"/v1/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json={"client_message_id": "client_0001", "text": "still stored"},
    )
    hidden = client.delete(
        f"/v1/human-conversations/{conversation['id']}/entry",
        headers=visitor_headers,
    )
    assert hidden.status_code == 200
    assert client.get("/v1/human-conversations", headers=visitor_headers).json()[
        "data"
    ]["items"] == []
    assert client.get(
        f"/v1/human-conversations/{conversation['id']}/messages",
        headers=visitor_headers,
    ).status_code == 404
    assert client.get(
        f"/v1/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
    ).status_code == 200
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM human_messages WHERE conversation_id = ?",
            (conversation["id"],),
        ).fetchone()["n"] == 1


def test_report_copies_immutable_evidence_and_optional_block(
    client, fresh_db, monkeypatch
):
    _enable(monkeypatch)
    owner_headers, owner_login = _login(client, "19965205001")
    visitor_headers, visitor_login = _login(client, "19965205002")
    owner = owner_login["platform_user"]["id"]
    visitor = visitor_login["platform_user"]["id"]
    visit, conversation = _active(owner, visitor)
    sent = client.post(
        f"/v1/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json={"client_message_id": "client_0001", "text": "需要留证"},
    ).json()["data"]["message"]
    report = client.post(
        f"/v1/human-conversations/{conversation['id']}/report",
        headers=visitor_headers,
        json={
            "message_id": sent["message_id"],
            "reason_code": "harassment",
            "details": "测试举报",
            "block": True,
        },
    )
    assert report.status_code == 200, report.text
    assert report.json()["data"]["blocked"] is True
    assert db.get_universe_visit(visit_id=visit["id"])["status"] == "blocked"
    with db.connect() as conn:
        stored = conn.execute("SELECT * FROM human_chat_reports").fetchone()
    snapshot = json.loads(stored["evidence_snapshot_json"])
    assert snapshot["reported_message"]["body_text"] == "需要留证"
    assert stored["status"] == "open"
    assert stored["retained_until"] is None


def test_pg_concurrent_same_client_message_inserts_once(fresh_db):
    if not is_postgres():
        pytest.skip("PG 并发权威门禁")
    owner = _user("19965206001")
    visitor = _user("19965206002")
    _visit, conversation = _active(owner, visitor)

    def send() -> bool:
        return CompanionWorldHumanChatService().send(
            owner,
            conversation_id=conversation["id"],
            client_message_id="client_0001",
            body_text="hello",
            now=NOW,
            write_enabled=True,
        )["created"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: send(), range(2)))
    assert sorted(results) == [False, True]
    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM human_messages WHERE conversation_id = ?",
            (conversation["id"],),
        ).fetchone()["n"]
    assert count == 1


def test_pg_send_vs_block_finishes_read_only_without_orphan(fresh_db):
    if not is_postgres():
        pytest.skip("PG 并发权威门禁")
    owner = _user("19965207001")
    visitor = _user("19965207002")
    visit, conversation = _active(owner, visitor)

    def send() -> str:
        try:
            CompanionWorldHumanChatService().send(
                owner,
                conversation_id=conversation["id"],
                client_message_id="client_0001",
                body_text="race",
                now=NOW,
                write_enabled=True,
            )
            return "sent"
        except HumanChatError as err:
            return err.code

    def block() -> str:
        CompanionWorldHumanChatService().block(
            visitor, conversation_id=conversation["id"], now=NOW
        )
        return "blocked"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(send), pool.submit(block)]
        results = [future.result() for future in futures]
    assert results[1] == "blocked"
    assert results[0] in {"sent", "human_chat_read_only"}
    assert db.get_universe_visit(visit_id=visit["id"])["status"] == "blocked"
    assert db.get_human_conversation_for_visit(
        visit_id=visit["id"]
    )["status"] == "read_only"
    with pytest.raises(HumanChatError) as exc:
        CompanionWorldHumanChatService().send(
            owner,
            conversation_id=conversation["id"],
            client_message_id="client_0002",
            body_text="after",
            now=NOW,
            write_enabled=True,
        )
    assert exc.value.code == "human_chat_read_only"


def test_pg_send_vs_exact_expiry_never_commits_message(fresh_db):
    if not is_postgres():
        pytest.skip("PG 并发权威门禁")
    owner = _user("19965208001")
    visitor = _user("19965208002")
    visit, conversation = _active(owner, visitor)
    with db.connect() as conn:
        conn.execute(
            "UPDATE universe_visits SET expires_at = ? WHERE id = ?",
            ("2026-07-23 12:00:00", visit["id"]),
        )

    def send() -> str:
        try:
            CompanionWorldHumanChatService().send(
                owner,
                conversation_id=conversation["id"],
                client_message_id="client_0001",
                body_text="too late",
                now=NOW,
                write_enabled=True,
            )
            return "sent"
        except HumanChatError as err:
            return err.code

    def expire() -> str:
        CompanionWorldVisitService().maintain_expiry_batch(now=NOW, batch_size=10)
        return "expired"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [pool.submit(send), pool.submit(expire)]
        results = [future.result() for future in outcomes]
    assert results == ["human_chat_read_only", "expired"]
    assert db.get_universe_visit(visit_id=visit["id"])["status"] == "expired"
    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM human_messages WHERE conversation_id = ?",
            (conversation["id"],),
        ).fetchone()["n"]
    assert count == 0
