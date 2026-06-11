import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


BRIDGE_HEADERS = {"Authorization": "Bearer test-secret"}
ACCOUNT_ID_RE = re.compile(r"^aid_[1-9]\d{8}$")


def _extract_js_function(script: str, name: str) -> str:
    """Extract a top-level JavaScript function body from a static HTML script."""
    start = script.index(f"function {name}(")
    brace = script.index("{", start)
    depth = 0
    for i in range(brace, len(script)):
        if script[i] == "{":
            depth += 1
        elif script[i] == "}":
            depth -= 1
            if depth == 0:
                return script[start : i + 1]
    raise AssertionError(f"function {name} not closed")


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


def _get_login_data(phone: str, client):
    """Login via OTP flow; return (session_headers, login_response_data)."""
    with patch("app.main._schedule_binding_wait"), patch(
        "app.main.start_weixin_qr_login",
        return_value={"qrDataUrl": "data:image/png;base64,ZmFrZQ==", "sessionKey": "login-init-key"},
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        res = client.post("/web/login", json={"phone": phone, "verified_token": _get_verified_token(phone)})
    assert res.status_code == 200, f"login failed: {res.text}"
    return {"Authorization": f"Bearer {res.json()['session_token']}"}, res.json()


def test_web_config_returns_public_captcha_settings(client, fresh_db):
    fresh_db.aliyun_captcha_scene_id = "scene-from-env"
    fresh_db.aliyun_captcha_prefix = "prefix-from-env"

    res = client.get("/web/config")

    assert res.status_code == 200
    data = res.json()
    assert data["captcha"] == {
        "provider": "aliyun",
        "scene_id": "scene-from-env",
        "prefix": "prefix-from-env",
        "configured": True,
    }
    assert "access_key" not in str(data).lower()


def test_web_config_marks_captcha_unconfigured(client):
    res = client.get("/web/config")

    assert res.status_code == 200
    data = res.json()
    assert data["captcha"]["configured"] is False
    assert data["captcha"]["scene_id"] == ""
    assert data["captcha"]["prefix"] == ""


def test_dashboard_page_hides_channel_account_identifier():
    html = Path("app/static/dashboard.html").read_text(encoding="utf-8")

    assert "微信账号标识" not in html
    assert "row-channel-account" not in html
    assert "d-channel-account" not in html


def test_dashboard_format_date_keeps_naive_beijing_time():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    html = Path("app/static/dashboard.html").read_text(encoding="utf-8")
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    probe = "\n".join(
        [
            _extract_js_function(script, "formatBeijingDate"),
            _extract_js_function(script, "formatDate"),
            """
const cases = [
  ['2026-06-11 22:30:00', '2026-06-11 22:30'],
  ['2026-06-11T14:30:00Z', '2026-06-11 22:30'],
  ['2026-06-11T22:30:00+08:00', '2026-06-11 22:30'],
];
for (const [input, expected] of cases) {
  const actual = formatDate(input);
  if (actual !== expected) {
    throw new Error(input + ' -> ' + actual + ', expected ' + expected);
  }
}
""",
        ]
    )
    res = subprocess.run([node, "-e", probe], capture_output=True, text=True, check=False)

    assert res.returncode == 0, res.stderr


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
    assert ACCOUNT_ID_RE.match(data["account"]["id"])
    assert data["account"]["display_name"] == "Bob Bot"
    assert data["account"]["channel"] == "openclaw-weixin"
    assert data["profile"]["account_id"] == data["account"]["id"]
    assert data["profile"]["system_prompt"] == "你是 Bob 的个人助理。"
    assert data["owner_binding"]["platform_user_id"] == user["id"]
    assert data["owner_binding"]["account_id"] == data["account"]["id"]
    assert data["owner_binding"]["binding_method"] == "web_onboarding"
    assert data["subscription"]["plan"] == "trial"


def test_web_create_binding_intent_starts_openclaw_qr_login(client):
    session_headers, login_data = _get_login_data("13800000002", client)
    user = login_data["platform_user"]
    account = login_data["account"]

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
            json={},
            headers=session_headers,
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
    assert ACCOUNT_ID_RE.match(data["account"]["id"])
    assert data["account"]["display_name"] is None
    assert data["profile"]["display_name"] is None
    assert data["owner_binding"]["platform_user_id"] == data["platform_user"]["id"]
    assert data["owner_binding"]["account_id"] == data["account"]["id"]
    assert data["subscription"]["plan"] == "free"
    assert data["wallet"]["display"]["balance"] == "1000"
    assert data["binding_intent"]["status"] == "qr_created"
    assert data["binding_intent"]["qr_data_url"] == "data:image/png;base64,ZmFrZQ=="
    assert data["binding_intent"]["account_id"] == data["account"]["id"]
    mock_start.assert_called_once()
    mock_schedule.assert_called_once_with(data["binding_intent"]["id"])


def test_default_account_context_has_no_ai4all_name(client):
    from app.user_profiles import read_agent_context

    session_headers, login_data = _get_login_data("13800000109", client)

    assert session_headers["Authorization"].startswith("Bearer ")
    assert login_data["account"]["display_name"] is None
    context = read_agent_context(
        login_data["account"]["id"],
        display_name=login_data["account"]["display_name"],
    )

    assert "AI4ALL 助手" not in context.blocks["IDENTITY"]
    assert "你还没有名字" in context.blocks["IDENTITY"]


def test_default_account_ignores_legacy_ai4all_display_name(fresh_db):
    from app.db import (
        create_or_get_platform_user_by_phone,
        get_or_create_default_ai4all_account_for_user,
    )

    user = create_or_get_platform_user_by_phone(phone="13800000119")
    result = get_or_create_default_ai4all_account_for_user(
        platform_user_id=user["id"],
        display_name="AI4ALL 助手",
    )

    assert result["account"]["display_name"] is None
    assert result["profile"]["display_name"] is None


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
    assert second["wallet"]["display"]["balance"] == "1000"
    with connect() as conn:
        active_account_count = conn.execute(
            """
            SELECT COUNT(*) FROM account_owner_bindings
            WHERE platform_user_id = ? AND status = 'active'
            """,
            (first["platform_user"]["id"],),
        ).fetchone()[0]
        ledger_count = conn.execute(
            """
            SELECT COUNT(*) FROM entitlement_ledger
            WHERE account_id = ?
              AND source_type = 'new_user_grant'
            """,
            (first["account"]["id"],),
        ).fetchone()[0]
    assert active_account_count == 1
    assert ledger_count == 1


def test_admin_account_includes_binding_diagnostics_for_web_account(client):
    with patch("app.main._schedule_binding_wait"), patch(
        "app.main.start_weixin_qr_login",
        return_value={"qrDataUrl": "data:image/png;base64,ZmFrZQ==", "sessionKey": "bind-admin-diag"},
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        data = client.post(
            "/web/register-and-binding-intent",
            json={
                "phone": "13800000919",
                "otp_token": _get_verified_token("13800000919"),
            },
        ).json()

    account_id = data["account"]["id"]
    res = client.get(
        f"/admin/accounts/{account_id}",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert res.status_code == 200
    detail = res.json()
    assert detail["platform_user"]["id"] == data["platform_user"]["id"]
    assert detail["platform_user"]["phone"] == "*******0919"
    assert detail["platform_user"]["phone_redacted"] is True
    assert detail["owner_bindings"][0]["account_id"] == account_id
    assert detail["owner_bindings"][0]["platform_user_id"] == data["platform_user"]["id"]
    assert detail["binding_intents"][0]["id"] == data["binding_intent"]["id"]
    assert detail["binding_intents"][0]["status"] == "qr_created"
    assert "qr_data_url" not in detail["binding_intents"][0]
    assert detail["binding_intents"][0]["qr_data_url_redacted"] is True
    assert "raw_result" not in detail["binding_intents"][0]


def test_web_me_wallet_returns_new_user_grant_and_is_idempotent(client):
    session_headers, login_data = _get_login_data("13800000209", client)

    assert login_data["wallet"]["display"]["balance"] == "1000"

    first = client.get("/web/me", headers=session_headers)
    second = client.get("/web/me/wallet", headers=session_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["wallet"]["display"]["balance"] == "1000"
    assert second.json()["wallet"]["display"]["balance"] == "1000"
    ledger = second.json()["ledger"]
    assert len(ledger) == 1
    assert ledger[0]["source_type"] == "new_user_grant"
    assert ledger[0]["amount_shells"] == "1000"


def test_admin_account_wallet_returns_balance_and_ledger(client):
    _, login_data = _get_login_data("13800000309", client)
    account_id = login_data["account"]["id"]

    res = client.get(
        f"/admin/accounts/{account_id}/wallet",
        headers={"Authorization": "Bearer test-admin"},
    )

    assert res.status_code == 200
    data = res.json()
    assert data["account_id"] == account_id
    assert data["wallet"]["display"]["balance"] == "1000"
    assert data["wallet"]["display"]["unit"] == "贝壳"
    assert data["ledger"][0]["source_type"] == "new_user_grant"
    assert data["ledger"][0]["amount_shells"] == "1000"
    assert data["redacted"] is True


def test_admin_account_wallet_is_read_only_and_does_not_duplicate_grant(client):
    from app.db import (
        create_ai4all_account_for_user,
        create_or_get_platform_user_by_phone,
        list_wallet_ledger,
    )

    user = create_or_get_platform_user_by_phone(phone="13800000310")
    account = create_ai4all_account_for_user(
        platform_user_id=user["id"],
        display_name="No Wallet Bot",
    )["account"]
    before = list_wallet_ledger(account_id=account["id"])

    res = client.get(
        f"/admin/accounts/{account['id']}/wallet",
        headers={"Authorization": "Bearer test-admin"},
    )
    after = list_wallet_ledger(account_id=account["id"])

    assert res.status_code == 200
    assert res.json()["wallet"]["display"]["balance"] == "1000"
    assert len(before) == 1
    assert len(after) == 1


def test_chat_turn_debits_wallet_balance_and_is_idempotent(client):
    from app.db import get_binding_intent
    from app.main import _complete_binding_intent_from_wait_result

    session_headers, login_data = _get_login_data("13800000210", client)
    account = login_data["account"]
    intent = login_data["binding_intent"]
    _complete_binding_intent_from_wait_result(
        get_binding_intent(binding_intent_id=intent["id"]),
        {"connected": True, "accountId": "wallet-bot@im.bot"},
    )

    payload = {
        "channel": "openclaw-weixin",
        "channel_account_id": "wallet-bot@im.bot",
        "account_id": "wallet-bot@im.bot",
        "session_key": intent["openclaw_login_session_key"],
        "sender_id": "peer-wallet",
        "chat_id": "chat-wallet",
        "chat_type": "private",
        "message_type": "text",
        "message_id": "wallet-msg-1",
        "text": "你好，帮我介绍一下今天可以做什么",
    }

    with patch("app.turn_service.generate_reply", return_value="可以先整理今天最重要的一件事。"):
        first = client.post("/openclaw/turn", json=payload, headers=BRIDGE_HEADERS)
        duplicate = client.post("/openclaw/turn", json=payload, headers=BRIDGE_HEADERS)

    assert first.status_code == 200
    assert first.json()["metadata"]["account_id"] == account["id"]
    assert first.json()["metadata"]["billing"]["charged"] is True
    assert first.json()["metadata"]["billing"]["amount_shells"].startswith("-")
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"

    wallet = client.get("/web/me/wallet", headers=session_headers)
    assert wallet.status_code == 200
    body = wallet.json()
    assert float(body["wallet"]["display"]["balance"]) < 1000
    usage_entries = [
        item for item in body["ledger"]
        if item["source_type"] == "usage_charge"
    ]
    assert len(usage_entries) == 1
    assert usage_entries[0]["entry_type"] == "debit"
    assert usage_entries[0]["amount_shells"].startswith("-")


def test_binding_wait_completion_binds_channel_account_to_precreated_account(client):
    from app.db import get_binding_intent, list_channel_bindings_for_account
    from app.main import _complete_binding_intent_from_wait_result

    session_headers, login_data = _get_login_data("13800000005", client)
    user = login_data["platform_user"]
    account = login_data["account"]

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
            json={},
            headers=session_headers,
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
    bound = [b for b in bindings if b["session_key"] == "bind-wait-session"]
    assert len(bound) == 1
    assert bound[0]["channel_account_id"] == "real-weixin-bot"
    assert bound[0]["raw_identity"]["platform_user_id"] == user["id"]


def test_binding_wait_already_connected_restores_local_binding(client):
    from app.db import get_binding_intent, list_channel_bindings_for_account
    from app.main import _complete_binding_intent_from_wait_result

    session_headers, login_data = _get_login_data("13800000211", client)
    account = login_data["account"]
    intent = login_data["binding_intent"]

    _complete_binding_intent_from_wait_result(
        get_binding_intent(binding_intent_id=intent["id"]),
        {
            "alreadyConnected": True,
            "accountId": "already-bot@im.bot",
            "message": "already connected",
        },
    )

    completed = get_binding_intent(binding_intent_id=intent["id"])
    assert completed["status"] == "completed"
    assert completed["raw_result"]["alreadyConnected"] is True
    bindings = list_channel_bindings_for_account(account_id=account["id"])
    assert len(bindings) == 1
    assert bindings[0]["channel_account_id"] == "already-bot@im.bot"
    assert bindings[0]["raw_identity"]["already_connected"] is True


def test_bound_channel_account_routes_turn_to_precreated_account(client):
    from app.db import get_binding_intent
    from app.main import _complete_binding_intent_from_wait_result

    session_headers, login_data = _get_login_data("13800000006", client)
    account = login_data["account"]

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
            json={},
            headers=session_headers,
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

    session_headers, login_data = _get_login_data("13800000008", client)
    account = login_data["account"]

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
            json={},
            headers=session_headers,
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

    session_headers, login_data = _get_login_data("13800000007", client)
    account = login_data["account"]

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
            json={},
            headers=session_headers,
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


def test_binding_intent_requires_session_auth(client):
    res = client.post("/web/binding-intents", json={})
    assert res.status_code == 401


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

    session_headers, login_data = _get_login_data("13800007777", client)

    with patch("app.main._schedule_binding_wait"), patch(
        "app.main.start_weixin_qr_login",
        return_value={"qrDataUrl": "data:image/png;base64,ZmFrZQ==", "sessionKey": "exp-session"},
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        intent = client.post(
            "/web/binding-intents",
            json={},
            headers=session_headers,
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

    session_headers, login_data = _get_login_data("13800006666", client)
    account = login_data["account"]

    with patch("app.main._schedule_binding_wait"), patch(
        "app.main.start_weixin_qr_login",
        return_value={"qrDataUrl": "data:image/png;base64,ZmFrZQ==", "sessionKey": "bind-dedup-session"},
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        intent = client.post(
            "/web/binding-intents",
            json={},
            headers=session_headers,
        ).json()["binding_intent"]

    _complete_binding_intent_from_wait_result(
        get_binding_intent(binding_intent_id=intent["id"]),
        {"connected": True, "accountId": "dedup-bot"},
    )

    # At this point one binding row exists with session_key="bind-dedup-session"
    bindings_after_qr = [
        b for b in list_channel_bindings_for_account(account_id=account["id"])
        if b["channel_account_id"] == "dedup-bot"
    ]
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

    bindings = [
        b for b in list_channel_bindings_for_account(account_id=account["id"])
        if b["channel_account_id"] == "dedup-bot"
    ]
    assert len(bindings) == 1, f"Expected 1 binding, got {len(bindings)}"
    assert bindings[0]["channel_account_id"] == "dedup-bot"
    assert bindings[0]["session_key"] == "agent:main:openclaw-weixin:dedup-bot:direct:peer"


def test_web_unbind_clear_all_allows_new_default_account(client):
    from app.db import connect

    session_headers, login_data = _get_login_data("13800000212", client)
    old_account_id = login_data["account"]["id"]

    with patch("app.main.logout_weixin_account") as mock_logout:
        res = client.post(
            "/web/me/unbind",
            headers=session_headers,
            json={"keep_memories": False},
        )

    assert res.status_code == 200
    assert res.json()["stats"]["entitlement_ledger_deleted"] == 1
    assert res.json()["stats"]["entitlement_wallets_deleted"] == 1
    assert res.json()["openclaw_cleanup"]["status"] == "skipped"
    mock_logout.assert_not_called()

    with connect() as conn:
        old_account = conn.execute(
            "SELECT status FROM accounts WHERE id = ?",
            (old_account_id,),
        ).fetchone()
        active_bindings = conn.execute(
            """
            SELECT COUNT(*) FROM account_owner_bindings
            WHERE account_id = ? AND status = 'active'
            """,
            (old_account_id,),
        ).fetchone()[0]
        wallet_count = conn.execute(
            "SELECT COUNT(*) FROM entitlement_wallets WHERE account_id = ?",
            (old_account_id,),
        ).fetchone()[0]

    assert old_account["status"] == "deactivated"
    assert active_bindings == 0
    assert wallet_count == 0

    # The still-valid server session can create a fresh default account; the
    # browser clears its local token after this flow, but this verifies DB state.
    me = client.get("/web/me", headers=session_headers)
    assert me.status_code == 200
    assert me.json()["account"]["id"] != old_account_id
    assert me.json()["wallet"]["display"]["balance"] == "1000"


def test_wipe_account_data_clears_tool_invocations_without_fk_error(fresh_db):
    """回归：wipe 必须先删 sessions 的子表 tool_invocations，否则 DELETE sessions
    触发 sqlite3.IntegrityError: FOREIGN KEY constraint failed（线上 500 根因）。"""
    from app.db import (
        connect,
        create_tool_invocation,
        get_account,
        get_or_create_session,
        wipe_account_data,
    )

    account_id = "aid_wipe_fk_test"
    state = get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="s1",
        sender_name=None,
        chat_id="c1",
        session_key="sk-wipe-fk",
    )
    session_id = state["session"]["id"]
    create_tool_invocation(
        account_id=account_id,
        tool_name="web_search",
        session_id=session_id,
        status="ok",
        finished=True,
    )

    # 修复前这里会抛 FK 异常；修复后正常返回并清空。
    stats = wipe_account_data(account_id=account_id)
    assert stats["tool_invocations_deleted"] >= 1
    assert stats["sessions_deleted"] >= 1

    assert get_account(account_id=account_id)["status"] == "deactivated"
    with connect() as conn:
        for table in ("tool_invocations", "sessions", "messages"):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE account_id = ?"
                if table != "messages"
                else "SELECT COUNT(*) FROM messages WHERE session_id IN "
                "(SELECT id FROM sessions WHERE account_id = ?)",
                (account_id,),
            ).fetchone()[0]
            assert n == 0, f"{table} not cleared"


def test_unbind_and_wipe_is_atomic_on_failure(fresh_db):
    """原子性回归：wipe 阶段失败时，unbind 阶段也必须整体回滚，
    不留「channel 已解绑但账号仍 active / 记忆仍在」的半成品。"""
    import app.db as db
    from app.db import (
        get_account,
        get_or_create_session,
        list_channel_bindings_for_account,
        unbind_and_wipe_account,
        upsert_channel_binding,
    )

    account_id = "aid_atomic_test"
    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="s1",
        sender_name=None,
        chat_id="c1",
        session_key="sk-atomic",
    )
    upsert_channel_binding(
        account_id=account_id,
        channel="openclaw-weixin",
        session_key="sk-atomic",
        channel_account_id="bot-atomic",
        sender_id="s1",
        chat_id="c1",
        raw_identity={},
    )
    assert list_channel_bindings_for_account(account_id=account_id)

    # 强制 wipe 阶段抛错（unbind 已在同一事务里执行但尚未提交）。
    with patch.object(db, "wipe_account_data", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            unbind_and_wipe_account(account_id=account_id)

    # 整体回滚：binding 仍在、账号仍未 deactivated。
    assert list_channel_bindings_for_account(account_id=account_id)
    assert get_account(account_id=account_id)["status"] != "deactivated"


def test_web_unbind_attempts_openclaw_weixin_logout(client):
    from app.db import get_binding_intent, list_channel_bindings_for_account
    from app.main import _complete_binding_intent_from_wait_result

    session_headers, login_data = _get_login_data("13800000213", client)
    account = login_data["account"]
    intent = login_data["binding_intent"]
    _complete_binding_intent_from_wait_result(
        get_binding_intent(binding_intent_id=intent["id"]),
        {"connected": True, "accountId": "logout-bot@im.bot"},
    )
    assert list_channel_bindings_for_account(account_id=account["id"])

    with patch("app.main.logout_weixin_account", return_value={"ok": True}) as mock_logout:
        res = client.post(
            "/web/me/unbind",
            headers=session_headers,
            json={"keep_memories": True},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["openclaw_cleanup"]["status"] == "ok"
    assert data["openclaw_cleanup"]["attempts"][0]["account_id"] == "logout-bot-im-bot"
    mock_logout.assert_called_once()
    assert mock_logout.call_args.kwargs["account_id"] == "logout-bot-im-bot"


def test_web_unbind_preserves_local_cleanup_when_openclaw_logout_unsupported(client):
    from app.db import get_binding_intent, list_channel_bindings_for_account
    from app.main import _complete_binding_intent_from_wait_result

    session_headers, login_data = _get_login_data("13800000214", client)
    account = login_data["account"]
    intent = login_data["binding_intent"]
    _complete_binding_intent_from_wait_result(
        get_binding_intent(binding_intent_id=intent["id"]),
        {"connected": True, "accountId": "unsupported-bot@im.bot"},
    )

    with patch(
        "app.main.logout_weixin_account",
        side_effect=RuntimeError('Channel "openclaw-weixin" does not support logout.'),
    ):
        res = client.post(
            "/web/me/unbind",
            headers=session_headers,
            json={"keep_memories": True},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["stats"]["channel_bindings_deleted"] == 1
    assert data["openclaw_cleanup"]["status"] == "unsupported"
    assert data["openclaw_cleanup"]["attempts"][0]["status"] == "unsupported"
    assert list_channel_bindings_for_account(account_id=account["id"]) == []
