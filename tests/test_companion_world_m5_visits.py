"""M5-2 invite/pending/accept API、双侧容量与 PostgreSQL 并发门禁。"""
from __future__ import annotations

import concurrent.futures
from datetime import datetime, timedelta

import pytest

import app.db as db
from app.db._backend import is_postgres
from app.platform.companion_world_visits import (
    CompanionWorldVisitService,
    VisitError,
)

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


def _enable(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.routers.companion_world.settings.companion_world_p1_enabled", True
    )
    monkeypatch.setattr(
        "app.routers.companion_world_visits.settings.companion_world_visits_enabled",
        True,
    )
    monkeypatch.setattr(
        "app.routers.companion_world_visits.beijing_naive_now", lambda: NOW
    )


def _user(phone: str) -> str:
    return db.create_or_get_platform_user_by_phone(phone=phone)["id"]


def _owner_invite(phone: str) -> tuple[str, dict]:
    owner = _user(phone)
    _confirm_world(owner)
    return owner, CompanionWorldVisitService().create_invite(owner, now=NOW)


def test_visit_flag_off_is_hidden(client):
    response = client.post("/v1/world/invites")
    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["code"] == "not_found"


def test_invite_redeem_requires_owner_accept_and_accept_is_atomic(
    client, fresh_db, monkeypatch
):
    _enable(monkeypatch)
    owner_headers, owner_login = _login(client, "19965101001")
    visitor_headers, visitor_login = _login(client, "19965101002")
    owner_id = owner_login["platform_user"]["id"]
    visitor_id = visitor_login["platform_user"]["id"]
    _confirm_world(owner_id)
    _confirm_world(visitor_id)

    created = client.post("/v1/world/invites", headers=owner_headers)
    assert created.status_code == 200, created.text
    invite = created.json()["data"]["invite"]
    assert len(invite["code"]) == 43
    assert invite["expires_at"] == "2026-07-24T12:00:00+08:00"
    with db.connect() as conn:
        stored = conn.execute(
            "SELECT code_hash, code_prefix FROM universe_invites WHERE id = ?",
            (invite["invite_id"],),
        ).fetchone()
    assert stored["code_hash"] != invite["code"]
    assert stored["code_prefix"] == invite["code"][:6]

    redeemed = client.post(
        "/v1/visits/redeem",
        headers=visitor_headers,
        json={"code": invite["code"]},
    )
    assert redeemed.status_code == 200, redeemed.text
    visit = redeemed.json()["data"]["visit"]
    assert visit["status"] == "pending"
    assert visit["pending_expires_at"] == "2026-07-30T12:00:00+08:00"
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM human_conversations"
        ).fetchone()["n"] == 0

    denied = client.post(
        f"/v1/visits/{visit['visit_id']}/accept", headers=visitor_headers
    )
    assert denied.status_code == 404
    assert denied.json()["code"] == "visit_not_found"

    accepted = client.post(
        f"/v1/visits/{visit['visit_id']}/accept", headers=owner_headers
    )
    assert accepted.status_code == 200, accepted.text
    accepted_data = accepted.json()["data"]
    assert accepted_data["visit"]["status"] == "active"
    assert accepted_data["visit"]["expires_at"] == "2026-08-22T12:00:00+08:00"
    assert accepted_data["replayed"] is False
    conversation_id = accepted_data["human_conversation_id"]

    replay = client.post(
        f"/v1/visits/{visit['visit_id']}/accept", headers=owner_headers
    )
    assert replay.status_code == 200
    assert replay.json()["data"]["human_conversation_id"] == conversation_id
    assert replay.json()["data"]["replayed"] is True

    left = client.post(
        f"/v1/visits/{visit['visit_id']}/leave", headers=visitor_headers
    )
    assert left.status_code == 200
    assert left.json()["data"]["visit"]["status"] == "left"
    with db.connect() as conn:
        conversation = conn.execute(
            "SELECT status FROM human_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        occupied = conn.execute(
            "SELECT COUNT(*) AS n FROM universe_visit_slots WHERE occupant_id = ?",
            (visit["visit_id"],),
        ).fetchone()["n"]
    assert conversation["status"] == "read_only"
    assert occupied == 0


def test_self_redeem_pending_cancel_and_owner_isolation(client, fresh_db, monkeypatch):
    _enable(monkeypatch)
    owner_headers, owner_login = _login(client, "19965102001")
    visitor_headers, _visitor_login = _login(client, "19965102002")
    owner_id = owner_login["platform_user"]["id"]
    _confirm_world(owner_id)

    first = client.post("/v1/world/invites", headers=owner_headers).json()["data"][
        "invite"
    ]
    self_redeem = client.post(
        "/v1/visits/redeem", headers=owner_headers, json={"code": first["code"]}
    )
    assert self_redeem.status_code == 403
    assert self_redeem.json()["code"] == "self_invite_not_allowed"

    redeemed = client.post(
        "/v1/visits/redeem", headers=visitor_headers, json={"code": first["code"]}
    ).json()["data"]["visit"]
    rejected_by_visitor = client.post(
        f"/v1/visits/{redeemed['visit_id']}/reject", headers=visitor_headers
    )
    assert rejected_by_visitor.status_code == 404
    cancelled = client.post(
        f"/v1/visits/{redeemed['visit_id']}/cancel", headers=visitor_headers
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["visit"]["status"] == "cancelled"

    replacement = client.post("/v1/world/invites", headers=owner_headers)
    assert replacement.status_code == 200


def test_world_three_slots_are_hard_cap_and_revoke_releases_slot(
    client, fresh_db, monkeypatch
):
    _enable(monkeypatch)
    headers, login = _login(client, "19965103001")
    _confirm_world(login["platform_user"]["id"])
    invites = [client.post("/v1/world/invites", headers=headers) for _ in range(3)]
    assert all(response.status_code == 200 for response in invites)
    full = client.post("/v1/world/invites", headers=headers)
    assert full.status_code == 409
    assert full.json()["code"] == "world_visit_limit_reached"

    invite_id = invites[0].json()["data"]["invite"]["invite_id"]
    revoked = client.delete(f"/v1/world/invites/{invite_id}", headers=headers)
    assert revoked.status_code == 200
    assert revoked.json()["data"]["invite"]["status"] == "revoked"
    assert client.post("/v1/world/invites", headers=headers).status_code == 200


def test_visitor_pending_plus_active_limit_is_three(fresh_db):
    visitor = _user("19965104000")
    service = CompanionWorldVisitService()
    invitations = [_owner_invite(f"1996510400{index}")[1] for index in range(1, 5)]
    visits = [
        service.redeem(visitor, code=item["code"], now=NOW)
        for item in invitations[:3]
    ]
    service.accept(
        invitations[0]["invite"]["owner_platform_user_id"],
        visit_id=visits[0]["id"],
        now=NOW,
    )
    with pytest.raises(VisitError) as exc:
        service.redeem(visitor, code=invitations[3]["code"], now=NOW)
    assert exc.value.code == "visitor_visit_limit_reached"


def test_exact_pending_expiry_is_terminal_and_releases_slot(fresh_db):
    owner, invitation = _owner_invite("19965104501")
    visitor = _user("19965104502")
    service = CompanionWorldVisitService()
    visit = service.redeem(visitor, code=invitation["code"], now=NOW)
    with pytest.raises(VisitError) as exc:
        service.accept(owner, visit_id=visit["id"], now=NOW + timedelta(days=7))
    assert exc.value.code == "visit_pending_expired"
    stored = db.get_universe_visit(visit_id=visit["id"])
    assert stored["status"] == "expired"
    with db.connect() as conn:
        occupied = conn.execute(
            "SELECT COUNT(*) AS n FROM universe_visit_slots WHERE occupant_id = ?",
            (visit["id"],),
        ).fetchone()["n"]
    assert occupied == 0


def test_pg_same_code_double_redeem_has_one_winner(fresh_db):
    if not is_postgres():
        pytest.skip("PG 并发权威门禁")
    _owner, invitation = _owner_invite("19965105001")
    visitors = [_user("19965105002"), _user("19965105003")]

    def redeem(visitor_id: str) -> str:
        try:
            CompanionWorldVisitService().redeem(
                visitor_id, code=invitation["code"], now=NOW
            )
            return "ok"
        except VisitError as err:
            return err.code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(redeem, visitors))
    assert results.count("ok") == 1
    assert results.count("invite_unavailable") == 1


def test_pg_visitor_third_fourth_competition_never_exceeds_three(fresh_db):
    if not is_postgres():
        pytest.skip("PG 并发权威门禁")
    visitor = _user("19965106000")
    invitations = [_owner_invite(f"1996510600{index}")[1] for index in range(1, 5)]
    service = CompanionWorldVisitService()
    for invitation in invitations[:2]:
        service.redeem(visitor, code=invitation["code"], now=NOW)

    def redeem(item: dict) -> str:
        try:
            CompanionWorldVisitService().redeem(
                visitor, code=item["code"], now=NOW
            )
            return "ok"
        except VisitError as err:
            return err.code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(redeem, invitations[2:]))
    assert sorted(results) == ["ok", "visitor_visit_limit_reached"]
    assert db.count_open_universe_visits_for_visitor(
        visitor_platform_user_id=visitor
    ) == 3


def test_pg_owner_third_fourth_slot_competition_never_exceeds_three(fresh_db):
    if not is_postgres():
        pytest.skip("PG 并发权威门禁")
    owner = _user("19965107001")
    _confirm_world(owner)
    service = CompanionWorldVisitService()
    service.create_invite(owner, now=NOW)
    service.create_invite(owner, now=NOW)

    def create() -> str:
        try:
            CompanionWorldVisitService().create_invite(owner, now=NOW)
            return "ok"
        except VisitError as err:
            return err.code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: create(), range(2)))
    assert sorted(results) == ["ok", "world_visit_limit_reached"]
    with db.connect() as conn:
        occupied = conn.execute(
            "SELECT COUNT(*) AS n FROM universe_visit_slots "
            "WHERE universe_id = (SELECT id FROM universes WHERE owner_platform_user_id = ?) "
            "AND occupant_id IS NOT NULL",
            (owner,),
        ).fetchone()["n"]
    assert occupied == 3


def test_pg_accept_vs_cancel_has_one_terminal_decision(fresh_db):
    if not is_postgres():
        pytest.skip("PG 并发权威门禁")
    owner, invitation = _owner_invite("19965108001")
    visitor = _user("19965108002")
    visit = CompanionWorldVisitService().redeem(
        visitor, code=invitation["code"], now=NOW
    )

    def accept() -> str:
        try:
            CompanionWorldVisitService().accept(owner, visit_id=visit["id"], now=NOW)
            return "accepted"
        except VisitError as err:
            return err.code

    def cancel() -> str:
        try:
            CompanionWorldVisitService().terminate(
                visitor, visit_id=visit["id"], action="cancel", now=NOW
            )
            return "cancelled"
        except VisitError as err:
            return err.code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        accept_future = pool.submit(accept)
        cancel_future = pool.submit(cancel)
        results = [accept_future.result(), cancel_future.result()]
    assert sum(item in {"accepted", "cancelled"} for item in results) == 1
    assert results.count("visit_not_pending") == 1
    stored = db.get_universe_visit(visit_id=visit["id"])
    assert stored["status"] in {"active", "cancelled"}
