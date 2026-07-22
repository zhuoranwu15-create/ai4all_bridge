"""M2-C C2 世界 API：flag、信封、bootstrap/confirm/create 与安全字段。"""
import json

import app.db as db


def _verified_token(phone: str) -> str:
    row = db.create_phone_verification(phone=phone, code="999999", expires_minutes=10)
    return db.set_verification_verified(row["id"], token_expires_minutes=10)[
        "verified_token"
    ]


def _login(client, phone: str) -> tuple[dict, dict]:
    response = client.post(
        "/v1/auth/session",
        json={"phone": phone, "verified_token": _verified_token(phone)},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    return {"Authorization": f"Bearer {payload['access_token']}"}, payload


def _seed_catalog() -> None:
    for rank in range(1, 5):
        db.create_character_template(
            template_id=f"tmpl_api_{rank}",
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


def test_world_api_flag_off_is_hidden_with_stable_envelope(client):
    response = client.post("/v1/worlds/home/bootstrap")
    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["code"] == "not_found"
    assert response.json()["request_id"].startswith("req_")
    assert response.json()["message"] is None


def test_new_auth_under_flag_has_null_account_and_no_runtime_side_effects(
    client, fresh_db
):
    fresh_db.companion_world_p1_enabled = True
    _headers, first = _login(client, "19930001001")
    _headers, second = _login(client, "19930001001")
    assert first["is_new_user"] is True and second["is_new_user"] is False
    assert first["account"] is None and second["account"] is None
    assert first["welcome_message"] is None
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM account_owner_bindings").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM entitlement_wallets").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM subscriptions").fetchone()["c"] == 0


def test_flag_on_existing_user_reuses_legacy_account(client, fresh_db):
    headers, legacy = _login(client, "19930001002")
    del headers
    legacy_id = legacy["account"]["id"]
    fresh_db.companion_world_p1_enabled = True
    _headers, current = _login(client, "19930001002")
    assert current["is_new_user"] is False
    assert current["account"]["id"] == legacy_id
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 1


def test_bootstrap_and_candidates_are_idempotent_and_hide_persona(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers, _login_payload = _login(client, "19930001003")

    first = client.post("/api/v1/worlds/home/bootstrap", headers=headers)
    second = client.post("/v1/worlds/home/bootstrap", headers=headers)
    listing = client.get("/v1/worlds/home/resident-candidates", headers=headers)
    assert first.status_code == second.status_code == listing.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    assert first.json()["code"] == "ok"
    first_candidates = first.json()["data"]["candidates"]
    second_candidates = second.json()["data"]["candidates"]
    assert len(first_candidates) == 4
    assert [item["template_id"] for item in first_candidates] == [
        item["template_id"] for item in second_candidates
    ]
    assert listing.json()["data"]["candidates"] == second_candidates
    assert "persona_seed_json" not in first.text
    assert "人格1" not in first.text


def test_confirm_subset_replay_retired_snapshot_and_owner_resident_list(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers, _ = _login(client, "19930001004")
    candidates = client.post(
        "/v1/worlds/home/bootstrap", headers=headers
    ).json()["data"]["candidates"]
    selected = candidates[0]
    with db.connect() as conn:
        conn.execute(
            "UPDATE character_templates SET status='retired' WHERE id=?",
            (selected["template_id"],),
        )
    body = {
        "selections": [
            {"template_id": selected["template_id"], "display_name": "我的角色"}
        ]
    }
    first = client.post("/v1/worlds/home/residents/confirm", headers=headers, json=body)
    replay = client.post("/v1/worlds/home/residents/confirm", headers=headers, json=body)
    listing = client.get("/v1/worlds/home/residents", headers=headers)
    assert first.status_code == replay.status_code == listing.status_code == 200
    residents = first.json()["data"]["residents"]
    assert len(residents) == 1 and residents[0]["name"] == "我的角色"
    assert replay.json()["data"]["residents"][0]["resident_id"] == residents[0]["resident_id"]
    assert listing.json()["data"]["residents"] == replay.json()["data"]["residents"]
    assert "runtime_account_id" not in listing.text


def test_confirm_empty_and_invalid_body_use_stable_error_envelopes(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers, _ = _login(client, "19930001005")
    client.post("/v1/worlds/home/bootstrap", headers=headers)

    empty = client.post(
        "/v1/worlds/home/residents/confirm",
        headers=headers,
        json={"selections": []},
    )
    invalid = client.post(
        "/v1/worlds/home/residents",
        headers=headers,
        json={"template_id": "tmpl_api_1", "name": "不能同时给"},
    )
    assert empty.status_code == 409
    assert empty.json()["code"] == "resident_capacity_empty"
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_request"
    assert invalid.headers["Cache-Control"] == "no-store"
    forbidden = client.post(
        "/v1/worlds/home/residents",
        headers=headers,
        json={"template_id": "tmpl_api_1", "account_id": "attacker-chosen"},
    )
    assert forbidden.status_code == 400
    assert forbidden.json()["code"] == "account_id_not_accepted"


def test_selecting_custom_candidate_is_limited_to_one(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers, _ = _login(client, "19930001006")
    client.post("/v1/worlds/home/bootstrap", headers=headers)
    first = client.post(
        "/v1/worlds/home/residents",
        headers=headers,
        json={"name": "自建角色", "persona_hint": "说话沉稳但有幽默感"},
    )
    second = client.post(
        "/v1/worlds/home/residents",
        headers=headers,
        json={"name": "第二个自建"},
    )
    assert first.status_code == 200
    assert first.json()["data"]["candidate"]["origin"] == "custom"
    assert second.status_code == 409
    assert second.json()["code"] == "custom_candidate_limit_exceeded"
