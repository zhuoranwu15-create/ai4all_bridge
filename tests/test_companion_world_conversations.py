"""M2-C C3：conversation list/history/turn、owner ACL、L3 注入与 single-flight。"""
import json

import app.db as db
from app.domains.companion_world import CompanionWorldService
from app.platform import SqlCompanionWorldRepository
from app.schemas import OpenClawTurnResponse


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
    data = response.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data


def _seed_catalog() -> None:
    for rank in range(1, 5):
        db.create_character_template(
            template_id=f"tmpl_conv_{rank}",
            source_type="operations",
            name=f"会话角色{rank}",
            avatar_ref=f"asset://conv-{rank}",
            summary=f"会话角色简介{rank}",
            tags_json=json.dumps(["a", "b", "c"]),
            persona_seed_json=json.dumps(
                {
                    "SOUL.md": f"# SOUL\n\n会话人格{rank}",
                    "IDENTITY.md": f"# IDENTITY\n\n会话角色{rank}",
                },
                ensure_ascii=False,
            ),
            persona_version=f"v{rank}",
            initial_candidate_rank=rank,
        )


def _bootstrap_confirm(client, headers: dict, count: int = 2) -> list[dict]:
    candidates = client.post(
        "/v1/worlds/home/bootstrap", headers=headers
    ).json()["data"]["candidates"]
    response = client.post(
        "/v1/worlds/home/residents/confirm",
        headers=headers,
        json={
            "selections": [
                {"template_id": item["template_id"]} for item in candidates[:count]
            ]
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["residents"]


def _target(user_id: str, conversation_id: str):
    return CompanionWorldService(SqlCompanionWorldRepository()).resolve_conversation(
        user_id, conversation_id
    )


def _session_message(
    account_id: str,
    *,
    session_key: str,
    message_id: str,
    content: str,
) -> int:
    session = db.get_or_create_session(
        account_id=account_id,
        channel="native" if "app" in session_key else "web",
        sender_id="u",
        sender_name=None,
        chat_id=None,
        session_key=session_key,
        business_day="2026-07-21",
    )["session"]
    db.insert_message(
        account_id=account_id,
        session_id=int(session["id"]),
        message_id=message_id,
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content=content,
    )
    return int(session["id"])


def test_conversation_list_preview_unread_and_cursor_are_owner_scoped(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers, login = _login(client, "19950001001")
    residents = _bootstrap_confirm(client, headers, 2)
    target = _target(login["platform_user"]["id"], residents[0]["conversation_id"])
    _session_message(
        target.runtime_account_id,
        session_key=db.APP_ACTIVE_SESSION_KEY,
        message_id="app-preview",
        content="App 预览内容",
    )
    _session_message(
        target.runtime_account_id,
        session_key=db.WEB_ACTIVE_SESSION_KEY,
        message_id="web-hidden-preview",
        content="Web 不得成为预览",
    )

    response = client.get("/v1/conversations?limit=1", headers=headers)
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data["items"]) == 1 and data["next_cursor"]
    assert data["items"][0]["unread"] == 0
    assert "runtime_account_id" not in response.text
    second = client.get(
        f"/v1/conversations?limit=1&cursor={data['next_cursor']}", headers=headers
    )
    assert second.status_code == 200 and len(second.json()["data"]["items"]) == 1
    previews = {
        item["last_preview"]
        for item in data["items"] + second.json()["data"]["items"]
    }
    assert "App 预览内容" in previews and "Web 不得成为预览" not in previews


def test_history_isolated_by_owner_resident_and_app_scope(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers_a, login_a = _login(client, "19950001002")
    headers_b, _login_b = _login(client, "19950001003")
    residents = _bootstrap_confirm(client, headers_a, 2)
    target_a = _target(login_a["platform_user"]["id"], residents[0]["conversation_id"])
    target_other = _target(
        login_a["platform_user"]["id"], residents[1]["conversation_id"]
    )
    _session_message(
        target_a.runtime_account_id,
        session_key=db.APP_ACTIVE_SESSION_KEY,
        message_id="a-visible",
        content="A resident 可见",
    )
    _session_message(
        target_a.runtime_account_id,
        session_key=db.WEB_ACTIVE_SESSION_KEY,
        message_id="a-web-hidden",
        content="A Web 隐藏",
    )
    _session_message(
        target_other.runtime_account_id,
        session_key=db.APP_ACTIVE_SESSION_KEY,
        message_id="other-hidden",
        content="另一个 resident 隐藏",
    )

    own = client.get(
        f"/v1/ai-conversations/{target_a.conversation_id}/messages",
        headers=headers_a,
    )
    denied = client.get(
        f"/v1/ai-conversations/{target_a.conversation_id}/messages",
        headers=headers_b,
    )
    assert own.status_code == 200
    assert [item["text"] for item in own.json()["data"]["messages"]] == [
        "A resident 可见"
    ]
    assert denied.status_code == 404
    assert denied.json()["code"] == "conversation_not_found"


def test_turn_adapter_injects_only_target_universe_l3(client, fresh_db, monkeypatch):
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers, login = _login(client, "19950001004")
    resident = _bootstrap_confirm(client, headers, 1)[0]
    target = _target(login["platform_user"]["id"], resident["conversation_id"])
    db.append_universe_fact(
        universe_id=target.universe_id,
        fact_type="user_preference",
        payload_json='{"food":"目标世界偏好"}',
        occurred_at="2026-07-21 10:00:00",
    )
    other_user = db.create_or_get_platform_user_by_phone(
        phone="19950001005", display_name="他人"
    )["id"]
    other_world = db.get_or_create_home_universe(platform_user_id=other_user)
    db.append_universe_fact(
        universe_id=other_world["id"],
        fact_type="user_preference",
        payload_json='{"food":"他人世界秘密"}',
        occurred_at="2026-07-21 10:00:00",
    )
    captured = {}

    def _fake_turn(ctx):
        captured["ctx"] = ctx
        return OpenClawTurnResponse(
            status="ok", reply="已收到", metadata={"reply_message_id": "reply-l3"}
        )

    monkeypatch.setattr("app.agent_runtime.adapter.run_turn_for_account", _fake_turn)
    response = client.post(
        f"/v1/ai-conversations/{target.conversation_id}/turn",
        headers=headers,
        json={"client_message_id": "client_l3_001", "text": "我喜欢什么？"},
    )
    assert response.status_code == 200
    ctx = captured["ctx"]
    assert ctx.account_id == target.runtime_account_id
    assert ctx.cap.active_session_key == db.APP_ACTIVE_SESSION_KEY
    assert len(ctx.extra_blocks) == 1
    assert "目标世界偏好" in ctx.extra_blocks[0].text
    assert "他人世界秘密" not in ctx.extra_blocks[0].text


def test_turn_persists_once_and_replay_is_deduplicated(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers, login = _login(client, "19950001006")
    resident = _bootstrap_confirm(client, headers, 1)[0]
    target = _target(login["platform_user"]["id"], resident["conversation_id"])
    body = {"client_message_id": "client_dedupe_01", "text": "在吗"}
    first = client.post(
        f"/v1/ai-conversations/{target.conversation_id}/turn",
        headers=headers,
        json=body,
    )
    replay = client.post(
        f"/v1/ai-conversations/{target.conversation_id}/turn",
        headers=headers,
        json=body,
    )
    assert first.status_code == replay.status_code == 200
    assert first.json()["data"]["reply"]["text"] == "mock reply"
    assert replay.json()["data"]["deduplicated"] is True
    with db.connect() as conn:
        inbound = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE account_id=? AND message_id=?",
            (
                target.runtime_account_id,
                f"app:{target.conversation_id}:client_dedupe_01",
            ),
        ).fetchone()["c"]
    assert inbound == 1
    history = client.get(
        f"/v1/ai-conversations/{target.conversation_id}/messages",
        headers=headers,
    )
    assert history.status_code == 200
    assert target.runtime_account_id not in history.text


def test_read_only_account_id_rejection_and_owner_turn_acl(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers_a, login_a = _login(client, "19950001007")
    headers_b, _ = _login(client, "19950001008")
    resident = _bootstrap_confirm(client, headers_a, 1)[0]
    target = _target(login_a["platform_user"]["id"], resident["conversation_id"])

    forbidden = client.post(
        f"/v1/ai-conversations/{target.conversation_id}/turn",
        headers=headers_a,
        json={
            "client_message_id": "client_forbid_1",
            "text": "hi",
            "account_id": target.runtime_account_id,
        },
    )
    denied = client.post(
        f"/v1/ai-conversations/{target.conversation_id}/turn",
        headers=headers_b,
        json={"client_message_id": "client_denied_1", "text": "hi"},
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE ai_conversations SET state='read_only' WHERE id=?",
            (target.conversation_id,),
        )
    read_only = client.post(
        f"/v1/ai-conversations/{target.conversation_id}/turn",
        headers=headers_a,
        json={"client_message_id": "client_readonly", "text": "hi"},
    )
    assert forbidden.status_code == 400
    assert forbidden.json()["code"] == "account_id_not_accepted"
    assert denied.status_code == 404 and denied.json()["code"] == "conversation_not_found"
    assert read_only.status_code == 409
    assert read_only.json()["code"] == "conversation_read_only"


def test_conversation_single_flight_lock_is_nonblocking_across_connections(fresh_db):
    conversation_id = "conv-lock-proof"
    with db.try_conversation_turn_lock(conversation_id) as first:
        assert first is True
        with db.try_conversation_turn_lock(conversation_id) as second:
            assert second is False
    with db.try_conversation_turn_lock(conversation_id) as after_release:
        assert after_release is True
