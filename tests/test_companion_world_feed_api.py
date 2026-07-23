"""M3-2 用户文字 Feed API：flag、直接发布、cursor、幂等与 owner 隔离。"""
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
            template_id=f"tmpl_feed_{rank}",
            source_type="operations",
            name=f"Feed角色{rank}",
            avatar_ref=f"asset://feed-{rank}",
            summary=f"Feed简介{rank}",
            tags_json=json.dumps(["温柔", "好奇", f"类型{rank}"], ensure_ascii=False),
            persona_seed_json=json.dumps(
                {
                    "SOUL.md": f"# SOUL\n\nFeed人格{rank}",
                    "IDENTITY.md": f"# IDENTITY\n\n- 你的名字是Feed角色{rank}",
                },
                ensure_ascii=False,
            ),
            persona_version=f"v{rank}",
            initial_candidate_rank=rank,
        )


def _ready_world(client, fresh_db, phone: str, *, seed: bool = False):
    fresh_db.companion_world_p1_enabled = True
    fresh_db.companion_world_feed_enabled = True
    if seed:
        _seed_catalog()
    headers, login = _login(client, phone)
    candidates = client.post(
        "/v1/worlds/home/bootstrap", headers=headers
    ).json()["data"]["candidates"]
    response = client.post(
        "/v1/worlds/home/residents/confirm",
        headers=headers,
        json={"selections": [{"template_id": candidates[0]["template_id"]}]},
    )
    assert response.status_code == 200, response.text
    return headers, login


def test_feed_requires_both_p1_and_feed_flags(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    response = client.get("/v1/worlds/home/feed")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert response.headers["Cache-Control"] == "no-store"

    fresh_db.companion_world_p1_enabled = False
    fresh_db.companion_world_feed_enabled = True
    response = client.get("/v1/worlds/home/feed")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_feed_rejects_unconfirmed_world(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    fresh_db.companion_world_feed_enabled = True
    headers, _ = _login(client, "19962001001")
    response = client.post(
        "/v1/worlds/home/feed/posts",
        headers=headers,
        json={"client_request_id": "feed_req_1001", "text": "世界还没准备好"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "world_not_ready"


def test_feed_publish_replay_conflict_and_public_dto(client, fresh_db):
    headers, _ = _ready_world(client, fresh_db, "19962001002", seed=True)
    body = {"client_request_id": "feed_req_1002", "text": "  今天心情很好。  "}
    first = client.post("/api/v1/worlds/home/feed/posts", headers=headers, json=body)
    replay = client.post("/v1/worlds/home/feed/posts", headers=headers, json=body)
    conflict = client.post(
        "/v1/worlds/home/feed/posts",
        headers=headers,
        json={"client_request_id": "feed_req_1002", "text": "换一段正文"},
    )

    assert first.status_code == 201 and replay.status_code == 200
    first_post = first.json()["data"]["post"]
    assert replay.json()["data"]["post"]["post_id"] == first_post["post_id"]
    assert first_post["content"] == {"type": "text", "text": "今天心情很好。"}
    assert first_post["author"]["type"] == "human"
    assert first_post["author"]["resident_id"] is None
    assert first_post["post_type"] == "normal"
    assert first_post["source"] == "user_post"
    assert first_post["published_at"].endswith("+08:00")
    assert first.headers["Cache-Control"] == "no-store"
    assert "runtime_account_id" not in first.text
    assert "request_fingerprint" not in first.text
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM universe_posts").fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM companion_world_outbox"
        ).fetchone()["c"] == 1


def test_feed_cursor_is_stable_and_invalid_cursor_fails(client, fresh_db):
    headers, _ = _ready_world(client, fresh_db, "19962001003", seed=True)
    created_ids = set()
    for index in range(3):
        response = client.post(
            "/v1/worlds/home/feed/posts",
            headers=headers,
            json={
                "client_request_id": f"feed_req_20{index:02d}",
                "text": f"同一秒分页动态 {index}",
            },
        )
        assert response.status_code == 201
        created_ids.add(response.json()["data"]["post"]["post_id"])

    seen = []
    cursor = None
    for _ in range(3):
        params = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/v1/worlds/home/feed", headers=headers, params=params)
        assert page.status_code == 200, page.text
        data = page.json()["data"]
        assert len(data["items"]) == 1
        seen.append(data["items"][0]["post_id"])
        cursor = data["next_cursor"]
    assert set(seen) == created_ids and len(seen) == len(set(seen))
    assert cursor is None

    invalid = client.get(
        "/v1/worlds/home/feed", headers=headers, params={"cursor": "not-base64!"}
    )
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "invalid_cursor"


def test_feed_isolated_by_platform_user_and_rejects_client_owner_fields(
    client, fresh_db
):
    headers_a, _ = _ready_world(client, fresh_db, "19962001004", seed=True)
    headers_b, _ = _ready_world(client, fresh_db, "19962001005")
    post_a = client.post(
        "/v1/worlds/home/feed/posts",
        headers=headers_a,
        json={"client_request_id": "feed_req_3001", "text": "只属于甲"},
    )
    post_b = client.post(
        "/v1/worlds/home/feed/posts",
        headers=headers_b,
        json={"client_request_id": "feed_req_3002", "text": "只属于乙"},
    )
    assert post_a.status_code == post_b.status_code == 201

    list_a = client.get("/v1/worlds/home/feed", headers=headers_a).json()["data"]["items"]
    list_b = client.get("/v1/worlds/home/feed", headers=headers_b).json()["data"]["items"]
    assert [item["content"]["text"] for item in list_a] == ["只属于甲"]
    assert [item["content"]["text"] for item in list_b] == ["只属于乙"]

    forbidden = client.post(
        "/v1/worlds/home/feed/posts",
        headers=headers_a,
        json={
            "client_request_id": "feed_req_3003",
            "text": "不能自选世界",
            "universe_id": "uni_attacker",
        },
    )
    whitespace = client.post(
        "/v1/worlds/home/feed/posts",
        headers=headers_a,
        json={"client_request_id": "feed_req_3004", "text": "   "},
    )
    assert forbidden.status_code == 400
    assert forbidden.json()["code"] == "account_id_not_accepted"
    assert whitespace.status_code == 422
    assert whitespace.json()["code"] == "invalid_request"
