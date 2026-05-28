from unittest.mock import patch


BRIDGE_HEADERS = {"Authorization": "Bearer test-secret"}


def _get_verified_token(phone: str) -> str:
    from app.db import (
        create_phone_verification,
        get_latest_active_verification,
        set_verification_verified,
    )
    create_phone_verification(phone=phone, code="999999", expires_minutes=10)
    v = get_latest_active_verification(phone)
    result = set_verification_verified(v["id"], token_expires_minutes=10)
    return result["verified_token"]


def test_web_register_creates_and_reuses_platform_user(client):
    first = client.post(
        "/web/register",
        json={"phone": "13800000000", "display_name": "Alice",
              "otp_token": _get_verified_token("13800000000")},
    )
    assert first.status_code == 200
    first_user = first.json()["platform_user"]
    assert first_user["id"].startswith("user_")
    assert first_user["phone"] == "13800000000"
    assert first_user["display_name"] == "Alice"

    second = client.post(
        "/web/register",
        json={"phone": "13800000000", "display_name": "Alice Updated",
              "otp_token": _get_verified_token("13800000000")},
    )
    assert second.status_code == 200
    second_user = second.json()["platform_user"]
    assert second_user["id"] == first_user["id"]
    assert second_user["display_name"] == "Alice Updated"


def test_web_register_rejects_invalid_phone(client):
    res = client.post("/web/register", json={"phone": "123", "otp_token": "fake-token"})

    assert res.status_code == 400
    assert "phone" in res.json()["detail"]


def test_web_register_normalizes_phone(client):
    res1 = client.post(
        "/web/register",
        json={"phone": "135-8888-8888", "display_name": "Phone Test",
              "otp_token": _get_verified_token("13588888888")},
    )
    assert res1.status_code == 200
    assert res1.json()["platform_user"]["phone"] == "13588888888"

    # Same number with spaces deduplicates to same user
    res2 = client.post("/web/register",
                       json={"phone": "135 8888 8888",
                             "otp_token": _get_verified_token("13588888888")})
    assert res2.status_code == 200
    assert res2.json()["platform_user"]["id"] == res1.json()["platform_user"]["id"]


def test_web_create_agent_creates_account_profile_owner_and_subscription(client):
    user = client.post(
        "/web/register",
        json={"phone": "13800000001", "display_name": "Bob",
              "otp_token": _get_verified_token("13800000001")},
    ).json()["platform_user"]

    res = client.post(
        "/web/agents",
        json={
            "platform_user_id": user["id"],
            "agent_name": "Bob Bot",
            "role_prompt": "你是 Bob 的个人助理。",
            "plan": "trial",
        },
    )

    assert res.status_code == 200
    data = res.json()
    assert data["account"]["id"].startswith("acct_")
    assert data["account"]["display_name"] == "Bob Bot"
    assert data["account"]["channel"] == "openclaw-weixin"
    assert data["profile"]["account_id"] == data["account"]["id"]
    assert data["profile"]["system_prompt"] == "你是 Bob 的个人助理。"
    assert data["owner_binding"]["platform_user_id"] == user["id"]
    assert data["owner_binding"]["account_id"] == data["account"]["id"]
    assert data["owner_binding"]["binding_method"] == "web_onboarding"
    assert data["subscription"]["plan"] == "trial"


def test_web_create_binding_intent_starts_openclaw_qr_login(client):
    user = client.post(
        "/web/register",
        json={"phone": "13800000002",
              "otp_token": _get_verified_token("13800000002")},
    ).json()["platform_user"]
    account = client.post(
        "/web/agents",
        json={
            "platform_user_id": user["id"],
            "agent_name": "Binding Bot",
        },
    ).json()["account"]

    with patch("app.main._schedule_binding_wait") as mock_schedule, patch(
        "app.main.start_weixin_qr_login",
        return_value={
            "qrDataUrl": "data:image/png;base64,ZmFrZQ==",
            "rawQrDataUrl": "data:image/png;base64,ZmFrZQ==",
            "sessionKey": "bind-returned",
            "message": "用手机微信扫描以下二维码，以继续连接：",
        },
    ) as mock_start, patch("app.main.settings.openclaw_login_auto_start", True):
        res = client.post(
            "/web/binding-intents",
            json={
                "platform_user_id": user["id"],
                "account_id": account["id"],
            },
        )

    assert res.status_code == 200
    intent = res.json()["binding_intent"]
    assert intent["id"].startswith("bind_")
    assert intent["status"] == "qr_created"
    assert intent["platform_user_id"] == user["id"]
    assert intent["account_id"] == account["id"]
    assert intent["openclaw_login_session_key"] == "bind-returned"
    assert intent["qr_data_url"] == "data:image/png;base64,ZmFrZQ=="
    assert f"--account {intent['id']}" in intent["manual_login_command"]
    assert "--channel openclaw-weixin" in intent["manual_login_command"]
    mock_start.assert_called_once()
    assert mock_start.call_args.kwargs["account_id"] == intent["id"]
    mock_schedule.assert_called_once_with(intent["id"])

    fetched = client.get(f"/web/binding-intents/{intent['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["binding_intent"]["id"] == intent["id"]
    assert fetched.json()["binding_intent"]["qr_data_url"] == "data:image/png;base64,ZmFrZQ=="


def test_register_and_binding_intent_creates_default_account_and_qr(client):
    token = _get_verified_token("13800000009")

    with patch("app.main._schedule_binding_wait") as mock_schedule, patch(
        "app.main.start_weixin_qr_login",
        return_value={
            "qrDataUrl": "data:image/png;base64,ZmFrZQ==",
            "sessionKey": "bind-combined",
            "message": "scan",
        },
    ) as mock_start, patch("app.main.settings.openclaw_login_auto_start", True):
        res = client.post(
            "/web/register-and-binding-intent",
            json={"phone": "13800000009", "otp_token": token},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["platform_user"]["phone"] == "13800000009"
    assert data["account"]["id"].startswith("acct_")
    assert data["account"]["display_name"] == "AI4ALL 助手"
    assert data["owner_binding"]["platform_user_id"] == data["platform_user"]["id"]
    assert data["owner_binding"]["account_id"] == data["account"]["id"]
    assert data["subscription"]["plan"] == "free"
    assert data["binding_intent"]["status"] == "qr_created"
    assert data["binding_intent"]["qr_data_url"] == "data:image/png;base64,ZmFrZQ=="
    assert data["binding_intent"]["account_id"] == data["account"]["id"]
    mock_start.assert_called_once()
    mock_schedule.assert_called_once_with(data["binding_intent"]["id"])


def test_register_and_binding_intent_reuses_existing_default_account(client):
    from app.db import connect

    with patch("app.main._schedule_binding_wait"), patch(
        "app.main.start_weixin_qr_login",
        return_value={"qrDataUrl": "data:image/png;base64,ZmFrZQ==", "sessionKey": "bind-first"},
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        first = client.post(
            "/web/register-and-binding-intent",
            json={
                "phone": "13800000019",
                "otp_token": _get_verified_token("13800000019"),
            },
        ).json()

    with patch("app.main._schedule_binding_wait"), patch(
        "app.main.start_weixin_qr_login",
        return_value={"qrDataUrl": "data:image/png;base64,ZmFrZQ==", "sessionKey": "bind-second"},
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        second = client.post(
            "/web/register-and-binding-intent",
            json={
                "phone": "13800000019",
                "otp_token": _get_verified_token("13800000019"),
            },
        ).json()

    assert second["account"]["id"] == first["account"]["id"]
    assert second["binding_intent"]["id"] != first["binding_intent"]["id"]
    with connect() as conn:
        active_account_count = conn.execute(
            """
            SELECT COUNT(*) FROM account_owner_bindings
            WHERE platform_user_id = ? AND status = 'active'
            """,
            (first["platform_user"]["id"],),
        ).fetchone()[0]
    assert active_account_count == 1


def test_binding_wait_completion_binds_channel_account_to_precreated_account(client):
    from app.db import get_binding_intent, list_channel_bindings_for_account
    from app.main import _complete_binding_intent_from_wait_result

    user = client.post(
        "/web/register",
        json={"phone": "13800000005",
              "otp_token": _get_verified_token("13800000005")},
    ).json()["platform_user"]
    account = client.post(
        "/web/agents",
        json={
            "platform_user_id": user["id"],
            "agent_name": "Completed Binding Bot",
        },
    ).json()["account"]
    with patch("app.main._schedule_binding_wait"), patch(
        "app.main.start_weixin_qr_login",
        return_value={
            "qrDataUrl": "data:image/png;base64,ZmFrZQ==",
            "sessionKey": "bind-wait-session",
            "message": "scan",
        },
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        intent = client.post(
            "/web/binding-intents",
            json={
                "platform_user_id": user["id"],
                "account_id": account["id"],
            },
        ).json()["binding_intent"]

    _complete_binding_intent_from_wait_result(
        get_binding_intent(binding_intent_id=intent["id"]),
        {
            "connected": True,
            "accountId": "real-weixin-bot",
            "message": "已将此 OpenClaw 连接到微信。",
        },
    )

    completed = get_binding_intent(binding_intent_id=intent["id"])
    assert completed["status"] == "completed"
    assert completed["completed_at"]
    assert completed["raw_result"]["channel_account_id"] == "real-weixin-bot"

    bindings = list_channel_bindings_for_account(account_id=account["id"])
    assert len(bindings) == 1
    assert bindings[0]["session_key"] == "bind-wait-session"
    assert bindings[0]["channel_account_id"] == "real-weixin-bot"
    assert bindings[0]["raw_identity"]["platform_user_id"] == user["id"]


def test_bound_channel_account_routes_turn_to_precreated_account(client):
    from app.db import get_binding_intent
    from app.main import _complete_binding_intent_from_wait_result

    user = client.post(
        "/web/register",
        json={"phone": "13800000006",
              "otp_token": _get_verified_token("13800000006")},
    ).json()["platform_user"]
    account = client.post(
        "/web/agents",
        json={
            "platform_user_id": user["id"],
            "agent_name": "Routed Binding Bot",
        },
    ).json()["account"]
    with patch("app.main._schedule_binding_wait"), patch(
        "app.main.start_weixin_qr_login",
        return_value={
            "qrDataUrl": "data:image/png;base64,ZmFrZQ==",
            "sessionKey": "bind-route-session",
            "message": "scan",
        },
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        intent = client.post(
            "/web/binding-intents",
            json={
                "platform_user_id": user["id"],
                "account_id": account["id"],
            },
        ).json()["binding_intent"]
    _complete_binding_intent_from_wait_result(
        get_binding_intent(binding_intent_id=intent["id"]),
        {"connected": True, "accountId": "real-route-bot"},
    )

    res = client.post(
        "/openclaw/turn",
        headers=BRIDGE_HEADERS,
        json={
            "channel": "openclaw-weixin",
            "session_key": "agent:main:openclaw-weixin:real-route-bot:direct:peer-a",
            "channel_account_id": "real-route-bot",
            "account_id": "real-route-bot",
            "sender_id": "peer-a",
            "chat_id": "chat-a",
            "chat_type": "private",
            "message_type": "text",
            "message_id": "route-msg-1",
            "text": "#状态",
        },
    )

    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["metadata"]["account_id"] == account["id"]
    assert data["metadata"]["ai4all_account_id"] == account["id"]
    assert data["metadata"]["openclaw_session_key_account_id"] == "agent:main:openclaw-weixin:real-route-bot:direct:peer-a"
    assert data["metadata"]["channel_account_id"] == "real-route-bot"
    assert account["id"] in data["reply"]


def test_bound_weixin_normalized_channel_account_routes_to_precreated_account(client):
    from app.db import get_binding_intent
    from app.main import _complete_binding_intent_from_wait_result

    user = client.post(
        "/web/register",
        json={"phone": "13800000008",
              "otp_token": _get_verified_token("13800000008")},
    ).json()["platform_user"]
    account = client.post(
        "/web/agents",
        json={
            "platform_user_id": user["id"],
            "agent_name": "Normalized Weixin Bot",
        },
    ).json()["account"]
    with patch("app.main._schedule_binding_wait"), patch(
        "app.main.start_weixin_qr_login",
        return_value={
            "qrDataUrl": "data:image/png;base64,ZmFrZQ==",
            "sessionKey": "bind-normalized-route",
            "message": "scan",
        },
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        intent = client.post(
            "/web/binding-intents",
            json={
                "platform_user_id": user["id"],
                "account_id": account["id"],
            },
        ).json()["binding_intent"]
    _complete_binding_intent_from_wait_result(
        get_binding_intent(binding_intent_id=intent["id"]),
        {"connected": True, "accountId": "raw-bot@im.bot"},
    )

    res = client.post(
        "/openclaw/turn",
        headers=BRIDGE_HEADERS,
        json={
            "channel": "openclaw-weixin",
            "session_key": "agent:main:openclaw-weixin:raw-bot-im-bot:direct:peer-normalized",
            "channel_account_id": "raw-bot-im-bot",
            "account_id": "raw-bot-im-bot",
            "sender_id": "peer-normalized",
            "chat_id": "chat-normalized",
            "chat_type": "private",
            "message_type": "text",
            "message_id": "route-msg-normalized",
            "text": "#状态",
        },
    )

    assert res.status_code == 200
    data = res.json()
    assert data["metadata"]["account_id"] == account["id"]
    assert data["metadata"]["openclaw_session_key_account_id"] == (
        "agent:main:openclaw-weixin:raw-bot-im-bot:direct:peer-normalized"
    )
    assert account["id"] in data["reply"]


def test_bound_login_session_key_routes_turn_to_precreated_account(client):
    from app.db import get_binding_intent
    from app.main import _complete_binding_intent_from_wait_result

    user = client.post(
        "/web/register",
        json={"phone": "13800000007",
              "otp_token": _get_verified_token("13800000007")},
    ).json()["platform_user"]
    account = client.post(
        "/web/agents",
        json={
            "platform_user_id": user["id"],
            "agent_name": "Session Routed Bot",
        },
    ).json()["account"]
    with patch("app.main._schedule_binding_wait"), patch(
        "app.main.start_weixin_qr_login",
        return_value={
            "qrDataUrl": "data:image/png;base64,ZmFrZQ==",
            "sessionKey": "bind-session-route",
            "message": "scan",
        },
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        intent = client.post(
            "/web/binding-intents",
            json={
                "platform_user_id": user["id"],
                "account_id": account["id"],
            },
        ).json()["binding_intent"]
    _complete_binding_intent_from_wait_result(
        get_binding_intent(binding_intent_id=intent["id"]),
        {"connected": True, "accountId": "real-session-route-bot"},
    )

    res = client.post(
        "/openclaw/turn",
        headers=BRIDGE_HEADERS,
        json={
            "channel": "openclaw-weixin",
            "session_key": "bind-session-route",
            "channel_account_id": "bind-session-route",
            "account_id": "bind-session-route",
            "sender_id": "peer-b",
            "chat_type": "private",
            "message_type": "text",
            "message_id": "route-msg-2",
            "text": "#状态",
        },
    )

    assert res.status_code == 200
    data = res.json()
    assert data["metadata"]["account_id"] == account["id"]
    assert data["metadata"]["openclaw_session_key_account_id"] == "bind-session-route"


def test_binding_intent_requires_account_owned_by_platform_user(client):
    owner = client.post(
        "/web/register",
        json={"phone": "13800000003",
              "otp_token": _get_verified_token("13800000003")},
    ).json()["platform_user"]
    other = client.post(
        "/web/register",
        json={"phone": "13800000004",
              "otp_token": _get_verified_token("13800000004")},
    ).json()["platform_user"]
    account = client.post(
        "/web/agents",
        json={
            "platform_user_id": owner["id"],
            "agent_name": "Owner Bot",
        },
    ).json()["account"]

    res = client.post(
        "/web/binding-intents",
        json={
            "platform_user_id": other["id"],
            "account_id": account["id"],
        },
    )

    assert res.status_code == 400
    assert "not owned" in res.json()["detail"]


def test_web_create_agent_enforces_per_user_limit(client):
    user = client.post(
        "/web/register",
        json={"phone": "13800009999",
              "otp_token": _get_verified_token("13800009999")},
    ).json()["platform_user"]
    for i in range(10):
        res = client.post(
            "/web/agents",
            json={"platform_user_id": user["id"], "agent_name": f"Bot {i}"},
        )
        assert res.status_code == 200, f"agent {i} creation failed: {res.json()}"
    res = client.post(
        "/web/agents",
        json={"platform_user_id": user["id"], "agent_name": "Bot 11"},
    )
    assert res.status_code == 400
    assert "maximum" in res.json()["detail"]


def test_get_binding_intent_auto_expires_stale_qr(client):
    import app.db as db_module
    from app.db import get_binding_intent

    user = client.post(
        "/web/register",
        json={"phone": "13800007777",
              "otp_token": _get_verified_token("13800007777")},
    ).json()["platform_user"]
    account = client.post(
        "/web/agents",
        json={"platform_user_id": user["id"], "agent_name": "Expiry Bot"},
    ).json()["account"]
    with patch("app.main._schedule_binding_wait"), patch(
        "app.main.start_weixin_qr_login",
        return_value={"qrDataUrl": "data:image/png;base64,ZmFrZQ==", "sessionKey": "exp-session"},
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        intent = client.post(
            "/web/binding-intents",
            json={"platform_user_id": user["id"], "account_id": account["id"]},
        ).json()["binding_intent"]

    assert intent["status"] == "qr_created"

    # Force expires_at to the past
    with db_module.connect() as conn:
        conn.execute(
            "UPDATE binding_intents SET expires_at = datetime('now', '-1 minute') WHERE id = ?",
            (intent["id"],),
        )

    fetched = get_binding_intent(binding_intent_id=intent["id"])
    assert fetched["status"] == "expired"

    # Verify via HTTP too
    res = client.get(f"/web/binding-intents/{intent['id']}")
    assert res.status_code == 200
    assert res.json()["binding_intent"]["status"] == "expired"


def test_channel_binding_deduplicates_by_channel_account_id(client):
    from app.db import (
        get_binding_intent,
        list_channel_bindings_for_account,
        upsert_channel_binding,
    )
    from app.main import _complete_binding_intent_from_wait_result

    user = client.post(
        "/web/register",
        json={"phone": "13800006666",
              "otp_token": _get_verified_token("13800006666")},
    ).json()["platform_user"]
    account = client.post(
        "/web/agents",
        json={"platform_user_id": user["id"], "agent_name": "Dedup Bot"},
    ).json()["account"]
    with patch("app.main._schedule_binding_wait"), patch(
        "app.main.start_weixin_qr_login",
        return_value={"qrDataUrl": "data:image/png;base64,ZmFrZQ==", "sessionKey": "bind-dedup-session"},
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        intent = client.post(
            "/web/binding-intents",
            json={"platform_user_id": user["id"], "account_id": account["id"]},
        ).json()["binding_intent"]
    _complete_binding_intent_from_wait_result(
        get_binding_intent(binding_intent_id=intent["id"]),
        {"connected": True, "accountId": "dedup-bot"},
    )

    # At this point one binding row exists with session_key="bind-dedup-session"
    bindings_after_qr = list_channel_bindings_for_account(account_id=account["id"])
    assert len(bindings_after_qr) == 1

    # Simulate first inbound message: different session_key, same channel_account_id
    upsert_channel_binding(
        account_id=account["id"],
        channel="openclaw-weixin",
        session_key="agent:main:openclaw-weixin:dedup-bot:direct:peer",
        channel_account_id="dedup-bot",
        sender_id="peer",
        chat_id="chat-dedup",
    )

    bindings = list_channel_bindings_for_account(account_id=account["id"])
    assert len(bindings) == 1, f"Expected 1 binding, got {len(bindings)}"
    assert bindings[0]["channel_account_id"] == "dedup-bot"
    assert bindings[0]["session_key"] == "agent:main:openclaw-weixin:dedup-bot:direct:peer"
