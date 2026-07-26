"""S4 会话与 turn 契约：CONV-001 列表字段与 N+1、CONV-002 read cursor、TURN-001、TIME-001。

分四组：会话列表 DTO（时间/排序/可发送性）、已读游标语义与越权、turn 响应形状与幂等重放、
公开时间时区。账号隔离在每组都单独断言：他人会话一律 `conversation_not_found`，
未读与预览不跨 runtime account。
"""
import json

import app.db as db
from app.products.zhaoxi.application import SqlCompanionWorldRepository
from app.products.zhaoxi.domain.companion_world import CompanionWorldService


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
            template_id=f"tmpl_s4_{rank}",
            source_type="operations",
            name=f"S4角色{rank}",
            avatar_ref=f"asset://s4-{rank}",
            summary=f"S4简介{rank}",
            tags_json=json.dumps(["a", "b", "c"]),
            persona_seed_json=json.dumps(
                {"SOUL.md": f"# SOUL\n\nS4人格{rank}", "IDENTITY.md": "# IDENTITY\n\nS4"},
                ensure_ascii=False,
            ),
            persona_version=f"v{rank}",
            initial_candidate_rank=rank,
        )


def _confirm(client, headers: dict, count: int = 2) -> list[dict]:
    candidates = client.post("/v1/worlds/home/bootstrap", headers=headers).json()[
        "data"
    ]["candidates"]
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


def _app_message(account_id: str, *, role: str, content: str, message_id: str) -> int:
    """往 App scope 当前段写一条消息，返回其数值 id（客户端 read cursor 用的就是它）。"""
    session = db.get_or_create_session(
        account_id=account_id,
        channel="native",
        sender_id="u",
        sender_name=None,
        chat_id=None,
        session_key=db.APP_ACTIVE_SESSION_KEY,
        business_day="2026-07-26",
    )["session"]
    return int(
        db.insert_message(
            account_id=account_id,
            session_id=int(session["id"]),
            message_id=message_id,
            reply_to_message_id=None,
            direction="inbound" if role == "user" else "outbound",
            role=role,
            message_type="text",
            content=content,
        )
    )


def _items(client, headers: dict) -> dict:
    response = client.get("/v1/conversations", headers=headers)
    assert response.status_code == 200, response.text
    return {
        item["conversation_id"]: item for item in response.json()["data"]["items"]
    }


# --- CONV-001 会话列表 DTO -------------------------------------------------


def test_conversation_dto_exposes_times_and_send_state(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers, login = _login(client, "19960002001")
    residents = _confirm(client, headers, 2)
    chatted = _target(login["platform_user"]["id"], residents[0]["conversation_id"])
    _app_message(
        chatted.runtime_account_id,
        role="assistant",
        content="我在的",
        message_id="s4-a-1",
    )

    items = _items(client, headers)
    hot = items[residents[0]["conversation_id"]]
    cold = items[residents[1]["conversation_id"]]

    assert hot["last_message_at"] is not None and hot["last_message_at"].endswith(
        "+08:00"
    )
    assert hot["last_preview"] == "我在的"
    assert hot["can_send"] is True and hot["read_only_reason"] is None
    # 没聊过的居民不伪造消息时间/预览，但仍有稳定排序键（CONV-05/06）。
    assert cold["last_message_at"] is None and cold["last_preview"] is None
    assert cold["sort_time"] is not None and cold["sort_time"].endswith("+08:00")


def test_read_only_conversation_reports_reason_and_blocks_send(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers, login = _login(client, "19960002002")
    resident = _confirm(client, headers, 1)[0]
    target = _target(login["platform_user"]["id"], resident["conversation_id"])
    with db.connect() as conn:
        conn.execute(
            "UPDATE ai_conversations SET state='read_only' WHERE id=?",
            (target.conversation_id,),
        )

    item = _items(client, headers)[target.conversation_id]
    assert item["can_send"] is False
    assert item["read_only_reason"] == "resident_offline"
    denied = client.post(
        f"/v1/ai-conversations/{target.conversation_id}/turn",
        headers=headers,
        json={"client_message_id": "s4_readonly_1", "text": "hi"},
    )
    assert denied.status_code == 409
    assert denied.json()["code"] == "conversation_read_only"


def test_conversation_list_is_constant_query_count(client, fresh_db, monkeypatch):
    """CONV-001 的 N+1 收口：列表查询次数不随会话数增长。"""
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers, login = _login(client, "19960002003")
    residents = _confirm(client, headers, 4)
    for index, resident in enumerate(residents):
        target = _target(login["platform_user"]["id"], resident["conversation_id"])
        _app_message(
            target.runtime_account_id,
            role="assistant",
            content=f"回复{index}",
            message_id=f"s4-n-{index}",
        )

    from app.db import accounts as accounts_db

    calls = {"n": 0}
    original = accounts_db.summarize_app_conversations

    def _counting_summarize(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(accounts_db, "summarize_app_conversations", _counting_summarize)

    items = _items(client, headers)
    assert len(items) == 4
    # 4 个会话共用一次批量汇总；退回逐行查询会让这里变成 4。
    assert calls["n"] == 1
    assert {item["last_preview"] for item in items.values()} == {
        f"回复{index}" for index in range(4)
    }


def test_preview_and_unread_do_not_leak_across_residents(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers, login = _login(client, "19960002004")
    residents = _confirm(client, headers, 2)
    first = _target(login["platform_user"]["id"], residents[0]["conversation_id"])
    _app_message(
        first.runtime_account_id,
        role="assistant",
        content="只属于第一个居民",
        message_id="s4-iso-1",
    )

    items = _items(client, headers)
    assert items[residents[0]["conversation_id"]]["unread"] == 1
    assert items[residents[1]["conversation_id"]]["unread"] == 0
    assert items[residents[1]["conversation_id"]]["last_preview"] is None


# --- CONV-002 read cursor --------------------------------------------------


def test_unread_counts_assistant_messages_until_read(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers, login = _login(client, "19960002005")
    resident = _confirm(client, headers, 1)[0]
    target = _target(login["platform_user"]["id"], resident["conversation_id"])
    _app_message(
        target.runtime_account_id, role="user", content="我问的", message_id="s4-u-1"
    )
    first_ai = _app_message(
        target.runtime_account_id, role="assistant", content="第一条", message_id="s4-r-1"
    )
    _app_message(
        target.runtime_account_id, role="assistant", content="第二条", message_id="s4-r-2"
    )

    # 用户自己发的消息不计未读。
    assert _items(client, headers)[target.conversation_id]["unread"] == 2

    read = client.post(
        f"/v1/ai-conversations/{target.conversation_id}/read",
        headers=headers,
        json={"last_message_id": first_ai},
    )
    assert read.status_code == 200, read.text
    assert read.json()["data"]["unread"] == 1
    assert read.json()["data"]["last_read_message_id"] == first_ai
    assert _items(client, headers)[target.conversation_id]["unread"] == 1


def test_read_cursor_only_advances_and_clamps_to_latest(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers, login = _login(client, "19960002006")
    resident = _confirm(client, headers, 1)[0]
    target = _target(login["platform_user"]["id"], resident["conversation_id"])
    first = _app_message(
        target.runtime_account_id, role="assistant", content="一", message_id="s4-c-1"
    )
    latest = _app_message(
        target.runtime_account_id, role="assistant", content="二", message_id="s4-c-2"
    )

    def _read(last_message_id: int) -> dict:
        response = client.post(
            f"/v1/ai-conversations/{target.conversation_id}/read",
            headers=headers,
            json={"last_message_id": last_message_id},
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]

    # 传超大 id 只收敛到本会话真实最新一条，不会把未来消息也标已读。
    assert _read(10**9)["last_read_message_id"] == latest
    later = _app_message(
        target.runtime_account_id, role="assistant", content="三", message_id="s4-c-3"
    )
    assert _items(client, headers)[target.conversation_id]["unread"] == 1
    # 回退请求不生效，游标只前进。
    assert _read(first)["last_read_message_id"] == latest
    assert _read(later)["last_read_message_id"] == later
    assert _read(later)["unread"] == 0


def test_marking_read_does_not_reorder_the_list(client, fresh_db):
    """已读不是活动：标记已读不得改 sort_time，否则列表会跳序。"""
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers, login = _login(client, "19960002007")
    residents = _confirm(client, headers, 2)
    target = _target(login["platform_user"]["id"], residents[0]["conversation_id"])
    message_id = _app_message(
        target.runtime_account_id, role="assistant", content="旧的", message_id="s4-o-1"
    )
    before = [item["conversation_id"] for item in _items(client, headers).values()]
    sort_before = _items(client, headers)[target.conversation_id]["sort_time"]

    client.post(
        f"/v1/ai-conversations/{target.conversation_id}/read",
        headers=headers,
        json={"last_message_id": message_id},
    )

    after_items = _items(client, headers)
    assert [item["conversation_id"] for item in after_items.values()] == before
    assert after_items[target.conversation_id]["sort_time"] == sort_before


def test_read_endpoint_is_owner_scoped_and_validated(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers_a, login_a = _login(client, "19960002008")
    headers_b, _ = _login(client, "19960002009")
    resident = _confirm(client, headers_a, 1)[0]
    target = _target(login_a["platform_user"]["id"], resident["conversation_id"])

    denied = client.post(
        f"/v1/ai-conversations/{target.conversation_id}/read",
        headers=headers_b,
        json={"last_message_id": 1},
    )
    missing = client.post(
        "/v1/ai-conversations/conv-does-not-exist/read",
        headers=headers_a,
        json={"last_message_id": 1},
    )
    invalid = client.post(
        f"/v1/ai-conversations/{target.conversation_id}/read",
        headers=headers_a,
        json={"last_message_id": 0},
    )
    anonymous = client.post(
        f"/v1/ai-conversations/{target.conversation_id}/read",
        json={"last_message_id": 1},
    )

    # 越权与不存在同码，不泄漏他人会话的存在性。
    assert denied.status_code == 404 and denied.json()["code"] == "conversation_not_found"
    assert missing.status_code == 404
    assert missing.json()["code"] == "conversation_not_found"
    assert invalid.status_code == 422 and invalid.json()["code"] == "invalid_request"
    assert anonymous.status_code == 401


# --- TURN-001 turn 响应形状 ------------------------------------------------


def test_turn_replay_returns_the_same_persisted_message_id(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers, login = _login(client, "19960002010")
    resident = _confirm(client, headers, 1)[0]
    target = _target(login["platform_user"]["id"], resident["conversation_id"])
    body = {"client_message_id": "s4_replay_001", "text": "在吗"}

    first = client.post(
        f"/v1/ai-conversations/{target.conversation_id}/turn",
        headers=headers,
        json=body,
    ).json()["data"]
    replay = client.post(
        f"/v1/ai-conversations/{target.conversation_id}/turn",
        headers=headers,
        json=body,
    ).json()["data"]

    assert first["deduplicated"] is False and replay["deduplicated"] is True
    assert replay["reply"]["message_id"] == first["reply"]["message_id"]
    assert replay["reply"]["message_id"]
    assert replay["reply"]["text"] == first["reply"]["text"]


def test_no_reply_turn_returns_null_reply(client, fresh_db, monkeypatch):
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers, login = _login(client, "19960002011")
    resident = _confirm(client, headers, 1)[0]
    target = _target(login["platform_user"]["id"], resident["conversation_id"])

    from app.schemas import OpenClawTurnResponse

    def _silent_turn(ctx, *, product_services):
        return OpenClawTurnResponse(status="ok", reply=None, no_reply=True, metadata={})

    monkeypatch.setattr("app.agent_runtime.adapter.run_product_turn", _silent_turn)
    data = client.post(
        f"/v1/ai-conversations/{target.conversation_id}/turn",
        headers=headers,
        json={"client_message_id": "s4_silent_01", "text": "……"},
    ).json()["data"]

    # 冻结形状：no_reply 时整个 reply 为 null，不给「有对象但 text 为 null」的中间态。
    assert data["no_reply"] is True
    assert data["reply"] is None
    assert data["deduplicated"] is False


# --- TIME-001 公开时间 -----------------------------------------------------


def test_world_message_times_carry_offset(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    _seed_catalog()
    headers, login = _login(client, "19960002012")
    resident = _confirm(client, headers, 1)[0]
    target = _target(login["platform_user"]["id"], resident["conversation_id"])
    client.post(
        f"/v1/ai-conversations/{target.conversation_id}/turn",
        headers=headers,
        json={"client_message_id": "s4_time_0001", "text": "现在几点"},
    )

    world = client.get(
        f"/v1/ai-conversations/{target.conversation_id}/messages", headers=headers
    )
    assert world.status_code == 200
    times = [item["created_at"] for item in world.json()["data"]["messages"]]
    assert times and all(value.endswith("+08:00") for value in times)
