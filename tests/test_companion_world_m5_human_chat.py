"""M5-4 独立真人聊天、只读历史、self-hide、举报与 PG 发送竞态。"""
from __future__ import annotations

import concurrent.futures
import json
from datetime import datetime

import pytest

import app.db as db
from app.db._backend import is_postgres
from app.products.mingchan.application.human_chat import (
    CompanionWorldHumanChatService,
    HumanChatError,
)
from app.products.mingchan.application.visits import CompanionWorldVisitService

NOW = datetime(2026, 7, 23, 12, 0, 0)


def _login(client, phone: str) -> tuple[dict, dict]:
    verification = db.create_phone_verification(
        phone=phone, code="999999", expires_minutes=10
    )
    verified = db.set_verification_verified(
        verification["id"], token_expires_minutes=10
    )["verified_token"]
    response = client.post(
        "/api/v1/products/mingchan/auth/session", json={"phone": phone, "verified_token": verified}
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
        "app.products.mingchan.api.world.settings.mingchan_p1_enabled", True
    )
    monkeypatch.setattr(
        "app.products.mingchan.api.human_chat.settings.mingchan_human_chat_enabled",
        write,
    )
    monkeypatch.setattr(
        "app.products.mingchan.api.human_chat.beijing_naive_now", lambda: NOW
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

    listed = client.get("/api/v1/products/mingchan/human-conversations", headers=owner_headers)
    assert listed.status_code == 200
    assert listed.json()["data"]["items"][0]["conversation_id"] == conversation["id"]
    blocked = client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json={"client_message_id": "client_0001", "text": "hello"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "human_chat_read_only"
    history = client.get(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
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
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json={"client_message_id": "client_0001", "text": "  hello  "},
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["data"]["created"] is True
    message_id = sent.json()["data"]["message"]["message_id"]
    replay = client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json={"client_message_id": "client_0001", "text": "hello"},
    )
    assert replay.status_code == 200
    assert replay.json()["data"]["created"] is False
    assert replay.json()["data"]["message"]["message_id"] == message_id
    conflict = client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json={"client_message_id": "client_0001", "text": "changed"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"

    reply = client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=visitor_headers,
        json={"client_message_id": "client_0002", "text": "hi"},
    )
    assert reply.status_code == 200
    owner_history = client.get(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
    )
    assert [item["sender"] for item in owner_history.json()["data"]["items"]] == [
        "counterpart",
        "self",
    ]
    assert client.get(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
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
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json={"client_message_id": "client_0001", "text": "留存消息"},
    ).status_code == 200
    CompanionWorldVisitService().terminate(
        visitor, visit_id=visit["id"], action="leave", now=NOW
    )

    denied = client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=visitor_headers,
        json={"client_message_id": "client_0002", "text": "不能再发"},
    )
    assert denied.status_code == 409
    assert denied.json()["code"] == "human_chat_read_only"
    history = client.get(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
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
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json={"client_message_id": "client_0001", "text": "still stored"},
    )
    hidden = client.delete(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/entry",
        headers=visitor_headers,
    )
    assert hidden.status_code == 200
    assert client.get("/api/v1/products/mingchan/human-conversations", headers=visitor_headers).json()[
        "data"
    ]["items"] == []
    assert client.get(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=visitor_headers,
    ).status_code == 404
    assert client.get(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
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
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json={"client_message_id": "client_0001", "text": "需要留证"},
    ).json()["data"]["message"]
    report = client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/report",
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


# --- M5-REPORT-001 / M5-CONV-001：举报契约与会话列表读模型 -------------------


def test_report_options_are_versioned_and_match_accepted_codes(
    client, fresh_db, monkeypatch
):
    """契约表就是校验表：列出的每个码都必须真的被 report 接受。"""
    _enable(monkeypatch)
    owner_headers, owner_login = _login(client, "19965206001")
    visitor_headers, visitor_login = _login(client, "19965206002")
    owner = owner_login["platform_user"]["id"]
    visitor = visitor_login["platform_user"]["id"]
    _visit, conversation = _active(owner, visitor)

    options = client.get(
        "/api/v1/products/mingchan/human-conversations/report-options", headers=visitor_headers
    )
    assert options.status_code == 200, options.text
    assert options.headers["Cache-Control"] == "no-store"
    data = options.json()["data"]
    assert data["version"] >= 1
    codes = [option["reason_code"] for option in data["options"]]
    # 兜底项永远排最后，客户端据此渲染顺序。
    assert codes[-1] == "other"
    assert len(codes) == len(set(codes))
    for option in data["options"]:
        assert option["label"].strip()
        assert isinstance(option["details_required"], bool)

    # 未知码不得被接受，否则契约形同虚设。
    unknown = client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/report",
        headers=visitor_headers,
        json={"reason_code": "made_up_reason"},
    )
    assert unknown.status_code == 422
    assert unknown.json()["code"] == "invalid_request"

    # 契约里 details_required=false 的码必须真的能不带正文提交。
    optional_codes = [
        option["reason_code"]
        for option in data["options"]
        if not option["details_required"]
    ]
    assert optional_codes
    for index, code in enumerate(optional_codes):
        accepted = client.post(
            f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/report",
            headers=visitor_headers,
            json={"reason_code": code},
        )
        assert accepted.status_code == 200, f"{code}: {accepted.text}"


def test_report_enforces_details_required_codes(client, fresh_db, monkeypatch):
    """details_required=true 的码不带正文必须 422——契约说必填就得真校验。"""
    _enable(monkeypatch)
    owner_headers, owner_login = _login(client, "19965207001")
    visitor_headers, visitor_login = _login(client, "19965207002")
    owner = owner_login["platform_user"]["id"]
    visitor = visitor_login["platform_user"]["id"]
    _visit, conversation = _active(owner, visitor)

    options = client.get(
        "/api/v1/products/mingchan/human-conversations/report-options", headers=visitor_headers
    ).json()["data"]["options"]
    required = [
        option["reason_code"] for option in options if option["details_required"]
    ]
    assert required, "至少 other 应当要求正文"

    for code in required:
        missing = client.post(
            f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/report",
            headers=visitor_headers,
            json={"reason_code": code},
        )
        blank = client.post(
            f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/report",
            headers=visitor_headers,
            json={"reason_code": code, "details": "   "},
        )
        filled = client.post(
            f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/report",
            headers=visitor_headers,
            json={"reason_code": code, "details": "对方一直发无关内容"},
        )
        assert missing.status_code == 422, missing.text
        assert missing.json()["code"] == "invalid_request"
        # 纯空白等价于没填，不能靠空格绕过。
        assert blank.status_code == 422, blank.text
        assert filled.status_code == 200, filled.text

    with db.connect() as conn:
        rows = conn.execute("SELECT reason_code, details_text FROM human_chat_reports").fetchall()
    # 被拒的两次不得留下任何举报记录。
    assert len(rows) == len(required)


def test_report_options_available_when_send_flag_is_off(
    client, fresh_db, monkeypatch
):
    """human_chat_send=false 只关写入；举报入口仍必须可用。"""
    _enable(monkeypatch, write=False)
    _owner_headers, owner_login = _login(client, "19965208001")
    visitor_headers, visitor_login = _login(client, "19965208002")
    _active(owner_login["platform_user"]["id"], visitor_login["platform_user"]["id"])

    options = client.get(
        "/api/v1/products/mingchan/human-conversations/report-options", headers=visitor_headers
    )
    assert options.status_code == 200
    assert options.json()["data"]["options"]


def test_conversation_list_unread_preview_and_read_marker(
    client, fresh_db, monkeypatch
):
    """未读只数对方的消息；标记已读后归零，自己发的永远不算未读。"""
    _enable(monkeypatch)
    owner_headers, owner_login = _login(client, "19965209001")
    visitor_headers, visitor_login = _login(client, "19965209002")
    owner = owner_login["platform_user"]["id"]
    visitor = visitor_login["platform_user"]["id"]
    _visit, conversation = _active(owner, visitor)

    def _item(headers) -> dict:
        listed = client.get("/api/v1/products/mingchan/human-conversations", headers=headers)
        assert listed.status_code == 200, listed.text
        return listed.json()["data"]["items"][0]

    # 一条消息都没有：预览为空、未读为 0，而不是缺字段。
    fresh = _item(owner_headers)
    assert fresh["last_preview"] is None
    assert fresh["unread_count"] == 0

    for index in range(3):
        assert client.post(
            f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
            headers=visitor_headers,
            json={"client_message_id": f"client_100{index}", "text": f"访客第{index}句"},
        ).status_code == 200

    owner_item = _item(owner_headers)
    visitor_item = _item(visitor_headers)
    assert owner_item["unread_count"] == 3
    assert owner_item["last_preview"] == "访客第2句"
    # 自己发的消息对自己永远不是未读。
    assert visitor_item["unread_count"] == 0
    assert visitor_item["last_preview"] == "访客第2句"

    assert client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/read", headers=owner_headers
    ).status_code == 200
    assert _item(owner_headers)["unread_count"] == 0

    # 已读之后对方再发，未读重新计数且只算新的那条。
    assert client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=visitor_headers,
        json={"client_message_id": "client_1003", "text": "已读之后的新消息"},
    ).status_code == 200
    after = _item(owner_headers)
    assert after["unread_count"] == 1
    assert after["last_preview"] == "已读之后的新消息"


def test_read_marker_uses_sequence_not_timestamp(client, fresh_db, monkeypatch):
    """m0052 回归：与标记已读同一秒到达的消息不得被吞掉。

    ``_enable`` 把 ``beijing_naive_now`` 钉死成常量 NOW，read marker 与消息 created_at
    因此严格同秒——这正是旧时间戳实现会漏计的场景。
    """
    _enable(monkeypatch)
    owner_headers, owner_login = _login(client, "19965210001")
    visitor_headers, visitor_login = _login(client, "19965210002")
    owner = owner_login["platform_user"]["id"]
    visitor = visitor_login["platform_user"]["id"]
    _visit, conversation = _active(owner, visitor)

    assert client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=visitor_headers,
        json={"client_message_id": "client_2001", "text": "已读前"},
    ).status_code == 200
    assert client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/read", headers=owner_headers
    ).status_code == 200
    # 同一秒内到达的下一条：时间戳比不出先后，序号可以。
    assert client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=visitor_headers,
        json={"client_message_id": "client_2002", "text": "同秒到达"},
    ).status_code == 200

    item = client.get("/api/v1/products/mingchan/human-conversations", headers=owner_headers).json()[
        "data"
    ]["items"][0]
    assert item["unread_count"] == 1
    assert item["last_preview"] == "同秒到达"

    # 游标只前进不回退：重复标记已读不会把未读数算成负或让它复活。
    assert client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/read", headers=owner_headers
    ).status_code == 200
    assert client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/read", headers=owner_headers
    ).status_code == 200
    assert (
        client.get("/api/v1/products/mingchan/human-conversations", headers=owner_headers).json()["data"][
            "items"
        ][0]["unread_count"]
        == 0
    )


def test_conversation_list_exposes_send_gate_and_expiry(
    client, fresh_db, monkeypatch
):
    """can_send/read_only_reason/expires_at 三件套覆盖终态与开关两种只读来源。"""
    _enable(monkeypatch)
    owner_headers, owner_login = _login(client, "19965211001")
    _visitor_headers, visitor_login = _login(client, "19965211002")
    owner = owner_login["platform_user"]["id"]
    visitor = visitor_login["platform_user"]["id"]
    visit, _conversation = _active(owner, visitor)

    item = client.get("/api/v1/products/mingchan/human-conversations", headers=owner_headers).json()[
        "data"
    ]["items"][0]
    assert item["can_send"] is True
    assert item["read_only_reason"] is None
    # active visit 必须带绝对到期时间，客户端不再自己 join visit 列表。
    assert item["expires_at"] and item["expires_at"].endswith("+08:00")

    # 关掉写开关：只读原因是可恢复的 feature_disabled，不是终态。
    monkeypatch.setattr(
        "app.products.mingchan.api.human_chat.settings"
        ".mingchan_human_chat_enabled",
        False,
    )
    gated = client.get("/api/v1/products/mingchan/human-conversations", headers=owner_headers).json()[
        "data"
    ]["items"][0]
    assert gated["can_send"] is False
    assert gated["read_only_reason"] == "feature_disabled"

    # visit 进终态后，即使开关重新打开也必须是 visit_ended——终态优先于开关。
    monkeypatch.setattr(
        "app.products.mingchan.api.human_chat.settings"
        ".mingchan_human_chat_enabled",
        True,
    )
    CompanionWorldVisitService().terminate(
        visitor, visit_id=visit["id"], action="leave", now=NOW
    )
    ended = client.get("/api/v1/products/mingchan/human-conversations", headers=owner_headers).json()[
        "data"
    ]["items"][0]
    assert ended["can_send"] is False
    assert ended["read_only_reason"] == "visit_ended"


def test_conversation_list_is_participant_scoped_and_preview_is_bounded(
    client, fresh_db, monkeypatch
):
    """列表按 participant 隔离；预览截断且折叠换行，不把 4000 字正文塞进列表。"""
    from app.products.mingchan.infrastructure.persistence.companion_world_human_chat import (
        HUMAN_CONVERSATION_PREVIEW_CHARS,
    )

    _enable(monkeypatch)
    owner_headers, owner_login = _login(client, "19965212001")
    visitor_headers, visitor_login = _login(client, "19965212002")
    stranger_headers, stranger_login = _login(client, "19965212003")
    owner = owner_login["platform_user"]["id"]
    visitor = visitor_login["platform_user"]["id"]
    _confirm_world(stranger_login["platform_user"]["id"])
    _visit, conversation = _active(owner, visitor)

    long_text = "第一行\n\n第二行   多空格" + "长" * 300
    assert client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=visitor_headers,
        json={"client_message_id": "client_3001", "text": long_text},
    ).status_code == 200

    preview = client.get("/api/v1/products/mingchan/human-conversations", headers=owner_headers).json()[
        "data"
    ]["items"][0]["last_preview"]
    assert len(preview) <= HUMAN_CONVERSATION_PREVIEW_CHARS
    assert "\n" not in preview
    assert preview.startswith("第一行 第二行 多空格")

    # 第三方看不到别人的会话，更看不到预览。
    stranger = client.get("/api/v1/products/mingchan/human-conversations", headers=stranger_headers)
    assert stranger.status_code == 200
    assert stranger.json()["data"]["items"] == []


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
@pytest.fixture
def client(mingchan_client):
    return mingchan_client
