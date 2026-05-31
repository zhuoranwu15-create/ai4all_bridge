import json


ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}
STAFF_HEADERS = {"Authorization": "Bearer test-staff"}
BRIDGE_HEADERS = {"Authorization": "Bearer test-secret"}


def _turn_payload(message_id: str, text: str) -> dict:
    return {
        "account_id": "privacy-bot",
        "session_key": "sk-privacy-bot",
        "sender_id": "sender-privacy",
        "chat_type": "private",
        "message_type": "text",
        "message_id": message_id,
        "text": text,
        "raw": {
            "event_type": "message",
            "content": text,
            "nested": {"text": text},
        },
    }


def test_admin_session_and_raw_message_views_are_redacted_by_default(client):
    secret_text = "我的银行卡号是 6222000012345678"
    res = client.post(
        "/openclaw/turn",
        json=_turn_payload("privacy-msg-1", secret_text),
        headers=BRIDGE_HEADERS,
    )
    assert res.status_code == 200

    from app.db import list_sessions_for_account

    session = list_sessions_for_account(account_id="sk-privacy-bot")[0]
    res = client.get(f"/admin/sessions/{session['id']}", headers=ADMIN_HEADERS)

    assert res.status_code == 200
    data = res.json()
    assert data["redacted"] is True
    assert data["messages"][0]["content_redacted"] is True
    assert secret_text not in json.dumps(data, ensure_ascii=False)

    message_id = data["messages"][0]["id"]
    res = client.get(f"/admin/messages/{message_id}/raw", headers=ADMIN_HEADERS)

    assert res.status_code == 200
    raw_view = res.json()
    assert raw_view["redacted"] is True
    assert raw_view["message"]["raw_redacted"] is True
    assert secret_text not in json.dumps(raw_view, ensure_ascii=False)


def test_debug_plaintext_global_switch_allows_plaintext_only_in_non_production(client, fresh_db):
    secret_text = "开发机明文开关可见内容"
    fresh_db.admin_debug_plaintext_enabled = True
    fresh_db.app_env = "test"

    res = client.post(
        "/openclaw/turn",
        json=_turn_payload("privacy-msg-debug-global", secret_text),
        headers=BRIDGE_HEADERS,
    )
    assert res.status_code == 200

    from app.db import list_sessions_for_account, list_session_messages

    session = list_sessions_for_account(account_id="sk-privacy-bot")[0]
    user_message = next(
        message
        for message in list_session_messages(session_id=session["id"])
        if message["message_id"] == "privacy-msg-debug-global"
    )

    res = client.get(f"/admin/sessions/{session['id']}", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["redacted"] is False
    assert data["plaintext_debug"] is True
    assert data["messages"][0]["content"] == secret_text

    res = client.get(f"/admin/messages/{user_message['id']}/raw", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    raw_view = res.json()
    assert raw_view["redacted"] is False
    assert raw_view["message"]["content"] == secret_text
    assert raw_view["message"]["raw"]["raw_payload"]["content"] == secret_text

    fresh_db.app_env = "production"
    res = client.get(f"/admin/messages/{user_message['id']}/raw", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    redacted = res.json()
    assert redacted["redacted"] is True
    assert secret_text not in json.dumps(redacted, ensure_ascii=False)


def test_debug_plaintext_allowlist_bypasses_redaction_for_test_accounts(client, fresh_db):
    fresh_db.admin_debug_plaintext_enabled = False
    fresh_db.app_env = "production"
    fresh_db.admin_debug_plaintext_account_allowlist = "sk-privacy-bot"

    allowed_text = "白名单测试账号可见"
    blocked_text = "非白名单账号不可见"

    res = client.post(
        "/openclaw/turn",
        json=_turn_payload("privacy-msg-allowlist", allowed_text),
        headers=BRIDGE_HEADERS,
    )
    assert res.status_code == 200

    res = client.post(
        "/openclaw/turn",
        json={
            **_turn_payload("privacy-msg-not-allowlisted", blocked_text),
            "account_id": "privacy-other",
            "session_key": "sk-privacy-other",
            "sender_id": "sender-privacy-other",
        },
        headers=BRIDGE_HEADERS,
    )
    assert res.status_code == 200

    from app.db import list_sessions_for_account, list_session_messages

    allowed_session = list_sessions_for_account(account_id="sk-privacy-bot")[0]
    allowed_message = next(
        message
        for message in list_session_messages(session_id=allowed_session["id"])
        if message["message_id"] == "privacy-msg-allowlist"
    )
    res = client.get(f"/admin/messages/{allowed_message['id']}/raw", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    allowed = res.json()
    assert allowed["redacted"] is False
    assert allowed["message"]["content"] == allowed_text

    blocked_session = list_sessions_for_account(account_id="sk-privacy-other")[0]
    blocked_message = next(
        message
        for message in list_session_messages(session_id=blocked_session["id"])
        if message["message_id"] == "privacy-msg-not-allowlisted"
    )
    res = client.get(f"/admin/messages/{blocked_message['id']}/raw", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    blocked = res.json()
    assert blocked["redacted"] is True
    assert blocked_text not in json.dumps(blocked, ensure_ascii=False)


def test_plaintext_message_view_returns_content_and_writes_audit_event(client):
    secret_text = "这是用户投诉排查需要看的上下文"
    res = client.post(
        "/openclaw/turn",
        json=_turn_payload("privacy-msg-2", secret_text),
        headers=BRIDGE_HEADERS,
    )
    assert res.status_code == 200

    from app.db import list_sessions_for_account, list_session_messages

    session = list_sessions_for_account(account_id="sk-privacy-bot")[0]
    user_message = next(
        message
        for message in list_session_messages(session_id=session["id"])
        if message["message_id"] == "privacy-msg-2"
    )
    res = client.get(
        f"/admin/plaintext/messages/{user_message['id']}/raw?reason=complaint",
        headers=ADMIN_HEADERS,
    )

    assert res.status_code == 200
    data = res.json()
    assert data["plaintext"] is True
    assert data["message"]["content"] == secret_text
    assert data["message"]["raw"]["raw_payload"]["content"] == secret_text

    res = client.get("/admin/access-events?plaintext=true", headers=ADMIN_HEADERS)

    assert res.status_code == 200
    events = res.json()["events"]
    assert events[0]["action"] == "view_message_raw"
    assert events[0]["resource_type"] == "message"
    assert events[0]["resource_id"] == str(user_message["id"])
    assert events[0]["account_id"] == "sk-privacy-bot"
    assert events[0]["plaintext"] is True
    assert events[0]["reason"] == "complaint"


def test_staff_plaintext_requires_approved_grant(client):
    secret_text = "staff 需要授权才能看的上下文"
    res = client.post(
        "/openclaw/turn",
        json=_turn_payload("privacy-msg-staff", secret_text),
        headers=BRIDGE_HEADERS,
    )
    assert res.status_code == 200

    from app.db import list_sessions_for_account, list_session_messages

    session = list_sessions_for_account(account_id="sk-privacy-bot")[0]
    user_message = next(
        message
        for message in list_session_messages(session_id=session["id"])
        if message["message_id"] == "privacy-msg-staff"
    )

    res = client.get(
        f"/admin/messages/{user_message['id']}/raw",
        headers=STAFF_HEADERS,
    )
    assert res.status_code == 200
    assert res.json()["redacted"] is True

    res = client.get(
        f"/admin/plaintext/messages/{user_message['id']}/raw",
        headers=STAFF_HEADERS,
    )
    assert res.status_code == 403

    res = client.post(
        "/admin/plaintext-grants",
        json={
            "reason": "用户投诉排查",
            "account_scope": ["sk-privacy-bot"],
            "resource_scope": ["message"],
        },
        headers=STAFF_HEADERS,
    )
    assert res.status_code == 200
    grant = res.json()["grant"]
    assert grant["status"] == "pending"
    assert grant["requester_admin_user_id"] == "staff"

    res = client.post(
        f"/admin/plaintext-grants/{grant['id']}/approve",
        headers=STAFF_HEADERS,
    )
    assert res.status_code == 403

    res = client.post(
        f"/admin/plaintext-grants/{grant['id']}/approve",
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200
    approved = res.json()["grant"]
    assert approved["status"] == "approved"
    assert approved["expires_at"]

    res = client.get(
        f"/admin/plaintext/messages/{user_message['id']}/raw?reason=complaint",
        headers=STAFF_HEADERS,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["message"]["content"] == secret_text

    res = client.get("/admin/access-events?plaintext=true", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    event = res.json()["events"][0]
    assert event["admin_user_id"] == "staff"
    assert event["grant_id"] == grant["id"]


def test_staff_plaintext_grant_is_scoped_and_revocable(client):
    res = client.post(
        "/openclaw/turn",
        json=_turn_payload("privacy-msg-scope", "scope secret"),
        headers=BRIDGE_HEADERS,
    )
    assert res.status_code == 200

    from app.db import list_sessions_for_account, list_session_messages

    session = list_sessions_for_account(account_id="sk-privacy-bot")[0]
    user_message = next(
        message
        for message in list_session_messages(session_id=session["id"])
        if message["message_id"] == "privacy-msg-scope"
    )

    res = client.post(
        "/admin/plaintext-grants",
        json={
            "reason": "只授权 trace",
            "account_scope": ["sk-privacy-bot"],
            "resource_scope": ["debug_trace"],
        },
        headers=STAFF_HEADERS,
    )
    assert res.status_code == 200
    grant = res.json()["grant"]
    res = client.post(
        f"/admin/plaintext-grants/{grant['id']}/approve",
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200

    res = client.get(
        f"/admin/plaintext/messages/{user_message['id']}/raw",
        headers=STAFF_HEADERS,
    )
    assert res.status_code == 403

    res = client.post(
        f"/admin/plaintext-grants/{grant['id']}/revoke",
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200
    assert res.json()["grant"]["status"] == "revoked"


def test_named_admin_users_are_exposed_and_staff_cannot_list_users(client):
    res = client.get("/admin/me", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    admin_user = res.json()["admin_user"]
    assert admin_user["id"] == "admin"
    assert admin_user["role"] == "admin"

    res = client.get("/admin/me", headers=STAFF_HEADERS)
    assert res.status_code == 200
    staff_user = res.json()["admin_user"]
    assert staff_user["id"] == "staff"
    assert staff_user["role"] == "staff"

    res = client.get("/admin/users", headers=STAFF_HEADERS)
    assert res.status_code == 403

    res = client.get("/admin/users", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    users = res.json()["admin_users"]
    assert {user["id"] for user in users} >= {"admin", "staff"}


def test_debug_trace_is_redacted_by_default_and_plaintext_is_audited(client, fresh_db):
    fresh_db.debug_trace_account_ids = "sk-privacy-trace"
    secret_text = "trace secret content"

    res = client.post(
        "/openclaw/turn",
        json={
            **_turn_payload("privacy-trace-msg", secret_text),
            "account_id": "privacy-trace",
            "session_key": "sk-privacy-trace",
        },
        headers=BRIDGE_HEADERS,
    )
    assert res.status_code == 200
    trace_id = res.json()["metadata"]["debug_trace_id"]
    assert trace_id

    res = client.get(f"/admin/debug/traces/{trace_id}", headers=ADMIN_HEADERS)

    assert res.status_code == 200
    redacted = res.json()
    assert redacted["redacted"] is True
    assert redacted["trace"]["messages_redacted"] is True
    assert secret_text not in json.dumps(redacted, ensure_ascii=False)

    res = client.get(
        f"/admin/plaintext/debug-traces/{trace_id}?reason=debug",
        headers=ADMIN_HEADERS,
    )

    assert res.status_code == 200
    plaintext = res.json()
    assert plaintext["plaintext"] is True
    assert plaintext["trace"]["messages"][1]["content"] == secret_text

    res = client.get("/admin/access-events?plaintext=true", headers=ADMIN_HEADERS)

    assert res.status_code == 200
    events = res.json()["events"]
    assert events[0]["action"] == "view_debug_trace"
    assert events[0]["resource_id"] == trace_id
    assert events[0]["reason"] == "debug"
