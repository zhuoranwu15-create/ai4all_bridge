"""M3-2 用户文字 Feed API：flag、直接发布、cursor、幂等与 owner 隔离。

M2 追加：主人删除自己的动态（FEED-MGMT-001）与隐藏 AI 居民动态（FEED-MGMT-002）。
"""
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


def _active_resident_id(owner_id: str) -> str:
    world = db.get_universe(owner_platform_user_id=owner_id)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM universe_residents "
            "WHERE universe_id = ? AND status = 'active' ORDER BY id LIMIT 1",
            (world["id"],),
        ).fetchone()
    assert row is not None, "确认居民后应存在 active resident"
    return str(row["id"])


def _ai_post(owner_id: str, *, text: str = "居民今天说了句话。", slot: str = "morning") -> dict:
    """走真实 claim + publish 造一条 AI 居民动态，不直接 INSERT 绕过状态机。"""
    world = db.get_universe(owner_platform_user_id=owner_id)
    post, created = db.claim_ai_feed_slot(
        universe_id=world["id"],
        author_resident_id=_active_resident_id(owner_id),
        ai_local_date="2026-07-28",
        ai_slot=slot,
        slot_window_end_at=f"2026-07-28 {'11' if slot == 'morning' else '21'}:00:00",
        claim_token=f"claim-{owner_id[-6:]}-{slot}",
        claimed_at=f"2026-07-28 {'09' if slot == 'morning' else '19'}:00:00",
    )
    assert post is not None and created is True
    published, _outbox = db.publish_ai_feed_post_with_outbox(
        post_id=post["id"],
        claim_token=post["claim_token"],
        text=text,
        published_at="2026-07-28 09:30:00",
        outbox_idempotency_key=f"world-post-published:v1:{post['id']}",
        payload={"post_id": post["id"], "universe_id": world["id"]},
    )
    return published


def _feed_post_ids(client, headers) -> list[str]:
    response = client.get("/v1/worlds/home/feed", headers=headers, params={"limit": 50})
    assert response.status_code == 200, response.text
    return [item["post_id"] for item in response.json()["data"]["items"]]


def test_feed_requires_both_p1_and_feed_flags(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    response = client.get("/v1/worlds/home/feed")
    assert response.status_code == 404
    assert response.json()["code"] == "feature_disabled"
    assert response.headers["Cache-Control"] == "no-store"

    fresh_db.companion_world_p1_enabled = False
    fresh_db.companion_world_feed_enabled = True
    response = client.get("/v1/worlds/home/feed")
    assert response.status_code == 404
    assert response.json()["code"] == "feature_disabled"


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


# --- FEED-MGMT-001 / 002：主人删除与隐藏 ------------------------------------


def test_feed_management_requires_both_p1_and_feed_flags(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    for response in (
        client.delete("/v1/worlds/home/feed/posts/post_x"),
        client.post("/v1/worlds/home/feed/posts/post_x/hide", json={}),
    ):
        assert response.status_code == 404
        assert response.json()["code"] == "feature_disabled"
        assert response.headers["Cache-Control"] == "no-store"


def test_owner_deletes_own_post_and_replay_is_idempotent(client, fresh_db):
    headers, _ = _ready_world(client, fresh_db, "19962001006", seed=True)
    created = client.post(
        "/v1/worlds/home/feed/posts",
        headers=headers,
        json={"client_request_id": "feed_req_4001", "text": "这条我要删掉"},
    )
    post_id = created.json()["data"]["post"]["post_id"]

    first = client.delete(f"/v1/worlds/home/feed/posts/{post_id}", headers=headers)
    assert first.status_code == 200, first.text
    assert first.headers["Cache-Control"] == "no-store"
    # 冻结 DTO 字段集：response_model 会过滤未声明字段，多一个少一个都要在这里显形。
    assert first.json()["data"] == {
        "post_id": post_id,
        "status": "deleted",
        "replayed": False,
    }
    assert _feed_post_ids(client, headers) == []

    # IDEM-002 回归：重放不复用首次时间戳，必须仍是 200 且标记 replayed。
    replay = client.delete(f"/v1/worlds/home/feed/posts/{post_id}", headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"] == {
        "post_id": post_id,
        "status": "deleted",
        "replayed": True,
    }
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM companion_world_outbox "
            "WHERE post_id = ? AND event_type = 'universe_post.deleted.v1'",
            (post_id,),
        ).fetchone()["c"] == 1


def test_owner_hides_ai_post_without_touching_resident_lifecycle(client, fresh_db):
    headers, login = _ready_world(client, fresh_db, "19962001007", seed=True)
    owner_id = login["platform_user"]["id"]
    ai_post = _ai_post(owner_id)
    mine = client.post(
        "/v1/worlds/home/feed/posts",
        headers=headers,
        json={"client_request_id": "feed_req_4002", "text": "我的动态要留着"},
    ).json()["data"]["post"]["post_id"]
    assert set(_feed_post_ids(client, headers)) == {ai_post["id"], mine}

    with db.connect() as conn:
        before = dict(
            conn.execute(
                "SELECT * FROM universe_residents WHERE id = ?",
                (ai_post["author_resident_id"],),
            ).fetchone()
        )

    hidden = client.post(
        f"/v1/worlds/home/feed/posts/{ai_post['id']}/hide", headers=headers, json={}
    )
    assert hidden.status_code == 200, hidden.text
    assert hidden.headers["Cache-Control"] == "no-store"
    assert hidden.json()["data"] == {
        "post_id": ai_post["id"],
        "status": "hidden",
        "replayed": False,
    }
    assert _feed_post_ids(client, headers) == [mine]

    replay = client.post(
        f"/v1/worlds/home/feed/posts/{ai_post['id']}/hide", headers=headers, json={}
    )
    assert replay.status_code == 200
    assert replay.json()["data"]["replayed"] is True
    assert replay.json()["data"]["status"] == "hidden"

    # 隐藏不得改动居民生命周期、会话或离开状态。
    with db.connect() as conn:
        after = dict(
            conn.execute(
                "SELECT * FROM universe_residents WHERE id = ?",
                (ai_post["author_resident_id"],),
            ).fetchone()
        )
    assert after == before


def test_delete_and_hide_do_not_cross_author_types(client, fresh_db):
    headers, login = _ready_world(client, fresh_db, "19962001008", seed=True)
    owner_id = login["platform_user"]["id"]
    ai_post = _ai_post(owner_id)
    mine = client.post(
        "/v1/worlds/home/feed/posts",
        headers=headers,
        json={"client_request_id": "feed_req_4003", "text": "我的动态"},
    ).json()["data"]["post"]["post_id"]

    # AI 动态必须走隐藏语义，不能被当成用户内容删除。
    wrong_delete = client.delete(
        f"/v1/worlds/home/feed/posts/{ai_post['id']}", headers=headers
    )
    # 自己的动态删就是删，不能用隐藏语义处理。
    wrong_hide = client.post(
        f"/v1/worlds/home/feed/posts/{mine}/hide", headers=headers, json={}
    )
    assert wrong_delete.status_code == 404
    assert wrong_delete.json()["code"] == "post_not_found"
    assert wrong_hide.status_code == 404
    assert wrong_hide.json()["code"] == "post_not_found"
    # 两次拒绝都不得产生副作用。
    assert set(_feed_post_ids(client, headers)) == {ai_post["id"], mine}


def test_hide_rejects_farewell_post(client, fresh_db):
    """离别动态同时是世界 readiness 信号，隐藏它会让整个 Feed 变 world_not_ready。"""
    headers, login = _ready_world(client, fresh_db, "19962001009", seed=True)
    ai_post = _ai_post(login["platform_user"]["id"])
    with db.connect() as conn:
        conn.execute(
            "UPDATE universe_posts SET post_type = 'farewell' WHERE id = ?",
            (ai_post["id"],),
        )

    response = client.post(
        f"/v1/worlds/home/feed/posts/{ai_post['id']}/hide", headers=headers, json={}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "post_not_hideable"
    assert _feed_post_ids(client, headers) == [ai_post["id"]]


def test_retire_is_owner_scoped_and_rejects_unknown_ids(client, fresh_db):
    headers_a, login_a = _ready_world(client, fresh_db, "19962001010", seed=True)
    headers_b, _ = _ready_world(client, fresh_db, "19962001011")
    ai_post_a = _ai_post(login_a["platform_user"]["id"])
    post_a = client.post(
        "/v1/worlds/home/feed/posts",
        headers=headers_a,
        json={"client_request_id": "feed_req_4004", "text": "甲的动态"},
    ).json()["data"]["post"]["post_id"]

    cross_delete = client.delete(
        f"/v1/worlds/home/feed/posts/{post_a}", headers=headers_b
    )
    cross_hide = client.post(
        f"/v1/worlds/home/feed/posts/{ai_post_a['id']}/hide", headers=headers_b, json={}
    )
    missing = client.delete("/v1/worlds/home/feed/posts/post_does_not_exist", headers=headers_a)
    malformed = client.delete(
        "/v1/worlds/home/feed/posts/not%20a%20valid%20id", headers=headers_a
    )

    for response in (cross_delete, cross_hide, missing, malformed):
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "post_not_found"
    # 跨 owner 操作不得影响目标世界。
    assert set(_feed_post_ids(client, headers_a)) == {ai_post_a["id"], post_a}


def test_hide_rejects_client_supplied_fields(client, fresh_db):
    headers, login = _ready_world(client, fresh_db, "19962001012", seed=True)
    ai_post = _ai_post(login["platform_user"]["id"])
    # 自带下架原因/居民状态一律 422：原因由服务端按语义硬编码，客户端不参与。
    reason = client.post(
        f"/v1/worlds/home/feed/posts/{ai_post['id']}/hide",
        headers=headers,
        json={"reason_code": "attacker"},
    )
    # 注入世界/账号锚点走既有的 400 口径（ERROR-001），与发帖一致。
    anchor = client.post(
        f"/v1/worlds/home/feed/posts/{ai_post['id']}/hide",
        headers=headers,
        json={"universe_id": "uni_attacker"},
    )
    assert reason.status_code == 422
    assert reason.json()["code"] == "invalid_request"
    assert anchor.status_code == 400
    assert anchor.json()["code"] == "account_id_not_accepted"
    assert _feed_post_ids(client, headers) == [ai_post["id"]]


def test_feed_cursor_survives_deletion_mid_pagination(client, fresh_db):
    headers, _ = _ready_world(client, fresh_db, "19962001013", seed=True)
    ids = []
    for index in range(3):
        response = client.post(
            "/v1/worlds/home/feed/posts",
            headers=headers,
            json={
                "client_request_id": f"feed_req_50{index:02d}",
                "text": f"分页删除 {index}",
            },
        )
        ids.append(response.json()["data"]["post"]["post_id"])

    first = client.get("/v1/worlds/home/feed", headers=headers, params={"limit": 1})
    cursor = first.json()["data"]["next_cursor"]
    seen = [first.json()["data"]["items"][0]["post_id"]]

    # 翻页途中删掉「还没翻到」的一条：keyset cursor 不移位，续用旧游标既不重复也不错页。
    victim = next(post_id for post_id in ids if post_id not in seen)
    assert client.delete(
        f"/v1/worlds/home/feed/posts/{victim}", headers=headers
    ).status_code == 200

    while cursor:
        page = client.get(
            "/v1/worlds/home/feed",
            headers=headers,
            params={"limit": 1, "cursor": cursor},
        )
        assert page.status_code == 200, page.text
        data = page.json()["data"]
        seen.extend(item["post_id"] for item in data["items"])
        cursor = data["next_cursor"]

    assert len(seen) == len(set(seen))
    assert victim not in seen
    assert set(seen) == set(ids) - {victim}
