import json
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
    with patch("app.routers.web._schedule_binding_wait"), patch(
        "app.openclaw_gateway.start_weixin_qr_login",
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


def test_dashboard_exposes_referral_invite_entry():
    html = Path("app/static/dashboard.html").read_text(encoding="utf-8")

    assert "/web/me/referral-code" in html
    assert "https://ai4company.top/?invite_code=" in html
    assert "复制链接" in html
    assert "复制分享文案" in html


def test_home_carries_invite_code_to_login():
    html = Path("app/static/home.html").read_text(encoding="utf-8")

    assert "invite-hint" in html
    assert "new URLSearchParams(window.location.search)" in html
    assert "payload.invite_code = inviteCode" in html
    assert "/web/login" in html


def test_home_carries_campaign_code_to_login():
    html = Path("app/static/home.html").read_text(encoding="utf-8")

    assert "params.get('campaign_code')" in html
    assert "payload.campaign_code = campaignCode" in html


def test_home_existing_session_without_binding_creates_qr():
    html = Path("app/static/home.html").read_text(encoding="utf-8")

    resume_fn = _extract_js_function(html, "resumeExistingSession")
    create_qr_fn = _extract_js_function(html, "createBindingQrForSession")
    login_fn = _extract_js_function(html, "doLogin")

    assert "apiFetch('/web/me')" in resume_fn
    assert "apiFetch('/web/me/bindings')" in resume_fn
    assert "await createBindingQrForSession()" in resume_fn
    assert "window.location.href = '/user/dashboard.html';" in resume_fn
    assert "apiFetch('/web/me').then" not in html

    assert "apiFetch('/web/binding-intents'" in create_qr_fn
    assert "showBindingQr(data.binding_intent)" in create_qr_fn
    assert "showBindingQr(data.binding_intent)" in login_fn


def test_create_binding_qr_reuses_cached_pending_intent():
    """resumeExistingSession 在每次刷新页面时都会调用 createBindingQrForSession；若每次都
    直接 POST /web/binding-intents，会对同一个还没扫码的用户重复建行 + 重复触发真实的
    OpenClaw 扫码会话。必须先尝试复用本 tab 内缓存的、还没过期/完成的上一个 intent。"""
    html = Path("app/static/home.html").read_text(encoding="utf-8")
    create_qr_fn = _extract_js_function(html, "createBindingQrForSession")

    assert "sessionStorage.getItem(BINDING_INTENT_CACHE_KEY)" in create_qr_fn
    assert "sessionStorage.setItem(BINDING_INTENT_CACHE_KEY" in create_qr_fn
    assert "showBindingQr(cached.binding_intent)" in create_qr_fn


def test_public_site_shows_beta_badge():
    for path in ["app/static/home.html", "app/static/faq.html", "app/static/dashboard.html"]:
        html = Path(path).read_text(encoding="utf-8")
        assert "Beta 测试版" in html

    css = Path("app/static/site.css").read_text(encoding="utf-8")
    assert ".beta-badge" in css


def test_onboarding_page_carries_campaign_code_to_register():
    html = Path("app/static/onboarding.html").read_text(encoding="utf-8")

    assert "location.search).get('campaign_code')" in html
    assert "campaign_code: normalizedCampaignCode() || null" in html
    assert "/web/register-and-binding-intent" in html


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


def test_invalid_invite_code_does_not_consume_otp_or_create_user(client):
    from app.db import get_platform_user_by_phone

    token = _get_verified_token("13800000301")
    rejected = client.post(
        "/web/register",
        json={
            "phone": "13800000301",
            "otp_token": token,
            "invite_code": "BADCODE",
        },
    )

    assert rejected.status_code == 400
    assert rejected.json()["detail"] == "invalid_invite_code"
    assert get_platform_user_by_phone(phone="13800000301") is None

    accepted = client.post(
        "/web/register",
        json={"phone": "13800000301", "otp_token": token},
    )
    assert accepted.status_code == 200
    assert accepted.json()["platform_user"]["phone"] == "13800000301"


def test_existing_phone_ignores_invalid_invite_code(client):
    first = client.post(
        "/web/register",
        json={
            "phone": "13800000302",
            "otp_token": _get_verified_token("13800000302"),
        },
    )
    assert first.status_code == 200

    second = client.post(
        "/web/register",
        json={
            "phone": "13800000302",
            "otp_token": _get_verified_token("13800000302"),
            "invite_code": "BADCODE",
        },
    )

    assert second.status_code == 200
    assert second.json()["platform_user"]["id"] == first.json()["platform_user"]["id"]


def test_full_invite_code_does_not_consume_otp(client):
    from app.db import connect, get_or_create_personal_referral_code_for_user

    inviter = client.post(
        "/web/register",
        json={
            "phone": "13800000303",
            "otp_token": _get_verified_token("13800000303"),
        },
    ).json()["platform_user"]
    code = get_or_create_personal_referral_code_for_user(
        platform_user_id=inviter["id"],
    )
    with connect() as conn:
        conn.execute(
            "UPDATE referral_codes SET max_uses = 1, used_count = 1 WHERE id = ?",
            (code["id"],),
        )

    token = _get_verified_token("13800000304")
    rejected = client.post(
        "/web/register",
        json={
            "phone": "13800000304",
            "otp_token": token,
            "invite_code": code["code"],
        },
    )
    assert rejected.status_code == 400
    assert rejected.json()["detail"] == "invalid_invite_code"

    accepted = client.post(
        "/web/register",
        json={"phone": "13800000304", "otp_token": token},
    )
    assert accepted.status_code == 200
    assert accepted.json()["platform_user"]["phone"] == "13800000304"


def test_web_login_invalid_invite_code_does_not_consume_otp_or_create_user(client):
    from app.db import get_platform_user_by_phone

    token = _get_verified_token("13800000305")
    rejected = client.post(
        "/web/login",
        json={
            "phone": "13800000305",
            "verified_token": token,
            "invite_code": "BADCODE",
        },
    )

    assert rejected.status_code == 400
    assert rejected.json()["detail"] == "invalid_invite_code"
    assert get_platform_user_by_phone(phone="13800000305") is None

    with patch("app.routers.web._schedule_binding_wait"), patch(
        "app.openclaw_gateway.start_weixin_qr_login",
        return_value={"qrDataUrl": "data:image/png;base64,ZmFrZQ==", "sessionKey": "login-token-reuse"},
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        accepted = client.post(
            "/web/login",
            json={"phone": "13800000305", "verified_token": token},
        )
    assert accepted.status_code == 200
    assert accepted.json()["platform_user"]["phone"] == "13800000305"


def test_web_login_with_invite_code_creates_referral_relationship(client):
    from app.db import (
        get_or_create_personal_referral_code_for_user,
        list_referral_relationships,
    )

    inviter = client.post(
        "/web/register",
        json={
            "phone": "13800000306",
            "otp_token": _get_verified_token("13800000306"),
        },
    ).json()["platform_user"]
    code = get_or_create_personal_referral_code_for_user(
        platform_user_id=inviter["id"],
    )

    with patch("app.routers.web._schedule_binding_wait"), patch(
        "app.openclaw_gateway.start_weixin_qr_login",
        return_value={"qrDataUrl": "data:image/png;base64,ZmFrZQ==", "sessionKey": "login-invite"},
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        res = client.post(
            "/web/login",
            json={
                "phone": "13800000307",
                "verified_token": _get_verified_token("13800000307"),
                "invite_code": code["code"].lower(),
            },
        )

    assert res.status_code == 200
    invitee = res.json()["platform_user"]
    relationships = list_referral_relationships(invitee_platform_user_id=invitee["id"])
    assert len(relationships) == 1
    assert relationships[0]["inviter_platform_user_id"] == inviter["id"]
    assert relationships[0]["status"] == "registered"


def test_web_agents_endpoint_removed(client):
    """A 收敛:多账号创建入口 POST /web/agents 已删除(§9.3 A6)——返回 404/405,不再建号。"""
    session_headers, login_data = _get_login_data("13800000001", client)
    user = login_data["platform_user"]
    res = client.post(
        "/web/agents",
        json={"platform_user_id": user["id"], "agent_name": "Bob Bot"},
        headers=session_headers,
    )
    assert res.status_code in (404, 405)


def test_web_create_binding_intent_starts_openclaw_qr_login(client):
    session_headers, login_data = _get_login_data("13800000002", client)
    user = login_data["platform_user"]
    account = login_data["account"]

    with patch("app.routers.web._schedule_binding_wait") as mock_schedule, patch(
        "app.openclaw_gateway.start_weixin_qr_login",
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

    fetched = client.get(f"/web/binding-intents/{intent['id']}", headers=session_headers)
    assert fetched.status_code == 200
    assert fetched.json()["binding_intent"]["id"] == intent["id"]
    assert fetched.json()["binding_intent"]["qr_data_url"] == "data:image/png;base64,ZmFrZQ=="

    # 无鉴权访问被拒绝（曾可枚举 capability URL 拿到 qr_data_url / manual_login_command）。
    assert client.get(f"/web/binding-intents/{intent['id']}").status_code == 401

    # 他人登录态访问非属主 intent：按 404 处理，不泄露存在性。
    other_headers, _ = _get_login_data("13800000099", client)
    cross = client.get(f"/web/binding-intents/{intent['id']}", headers=other_headers)
    assert cross.status_code == 404


def test_register_and_binding_intent_creates_default_account_and_qr(client):
    token = _get_verified_token("13800000009")

    with patch("app.routers.web._schedule_binding_wait") as mock_schedule, patch(
        "app.openclaw_gateway.start_weixin_qr_login",
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

    with patch("app.routers.web._schedule_binding_wait"), patch(
        "app.openclaw_gateway.start_weixin_qr_login",
        return_value={"qrDataUrl": "data:image/png;base64,ZmFrZQ==", "sessionKey": "bind-first"},
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        first = client.post(
            "/web/register-and-binding-intent",
            json={
                "phone": "13800000019",
                "otp_token": _get_verified_token("13800000019"),
            },
        ).json()

    with patch("app.routers.web._schedule_binding_wait"), patch(
        "app.openclaw_gateway.start_weixin_qr_login",
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
    with patch("app.routers.web._schedule_binding_wait"), patch(
        "app.openclaw_gateway.start_weixin_qr_login",
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


@pytest.mark.slow
def test_chat_turn_debits_wallet_balance_and_is_idempotent(client):
    from app.db import get_binding_intent
    from app.routers.web import _complete_binding_intent_from_wait_result

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


def test_referral_invite_rewards_inviter_after_three_meaningful_messages(client, fresh_db):
    from app.db import (
        connect,
        get_binding_intent,
        list_referral_relationships,
        set_account_onboarding_state,
    )
    from app.routers.web import _complete_binding_intent_from_wait_result

    fresh_db.rate_limit_daily = 0
    inviter_headers, inviter_login = _get_login_data("13800000310", client)
    inviter_account = inviter_login["account"]

    code_res = client.get("/web/me/referral-code", headers=inviter_headers)
    assert code_res.status_code == 200
    invite_code = code_res.json()["referral_code"]["code"]

    preview = client.get(f"/web/referral-codes/{invite_code}/preview")
    assert preview.status_code == 200
    assert preview.json()["valid"] is True
    assert preview.json()["code"] == invite_code

    with patch("app.routers.web._schedule_binding_wait"), patch(
        "app.openclaw_gateway.start_weixin_qr_login",
        return_value={
            "qrDataUrl": "data:image/png;base64,ZmFrZQ==",
            "sessionKey": "referral-bind-session",
            "message": "scan",
        },
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        invitee_res = client.post(
            "/web/register-and-binding-intent",
            json={
                "phone": "13800000311",
                "otp_token": _get_verified_token("13800000311"),
                "invite_code": invite_code.lower(),
            },
        )

    assert invitee_res.status_code == 200
    invitee_data = invitee_res.json()
    invitee_account = invitee_data["account"]
    relationships = list_referral_relationships(
        invitee_platform_user_id=invitee_data["platform_user"]["id"],
    )
    assert len(relationships) == 1
    assert relationships[0]["status"] == "registered"

    with connect() as conn:
        code_row = conn.execute(
            "SELECT used_count FROM referral_codes WHERE code = ?",
            (invite_code,),
        ).fetchone()
    assert code_row["used_count"] == 1

    _complete_binding_intent_from_wait_result(
        get_binding_intent(binding_intent_id=invitee_data["binding_intent"]["id"]),
        {"connected": True, "accountId": "referral-bot@im.bot"},
    )
    set_account_onboarding_state(account_id=invitee_account["id"], state="complete")

    payload_base = {
        "channel": "openclaw-weixin",
        "channel_account_id": "referral-bot@im.bot",
        "account_id": "referral-bot@im.bot",
        "session_key": "referral-bind-session",
        "sender_id": "peer-referral",
        "chat_id": "chat-referral",
        "chat_type": "private",
        "message_type": "text",
    }
    messages = [
        "我今天想聊一下最近的工作压力和情绪",
        "我希望你帮我制定一个学习计划",
        "周末我想安排一次长跑训练并复盘",
    ]
    for idx, text in enumerate(messages, start=1):
        payload = {
            **payload_base,
            "message_id": f"referral-msg-{idx}",
            "text": text,
        }
        res = client.post("/openclaw/turn", json=payload, headers=BRIDGE_HEADERS)
        assert res.status_code == 200

    duplicate = client.post(
        "/openclaw/turn",
        json={**payload_base, "message_id": "referral-msg-3", "text": messages[-1]},
        headers=BRIDGE_HEADERS,
    )
    assert duplicate.status_code == 200

    relationships = list_referral_relationships(
        invitee_platform_user_id=invitee_data["platform_user"]["id"],
    )
    assert relationships[0]["status"] == "rewarded"
    assert relationships[0]["review_status"] == "passed"
    assert relationships[0]["meaningful_message_count"] == 3
    assert relationships[0]["reward_ledger_id"]

    wallet = client.get("/web/me/wallet", headers=inviter_headers).json()
    assert wallet["account_id"] == inviter_account["id"]
    assert wallet["wallet"]["display"]["balance"] == "2000"
    reward_entries = [
        item for item in wallet["ledger"]
        if item["source_type"] == "referral_reward"
    ]
    assert len(reward_entries) == 1
    assert reward_entries[0]["amount_shells"] == "1000"

    admin = client.get(
        "/admin/referrals",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert admin.status_code == 200
    assert admin.json()["referrals"][0]["id"] == relationships[0]["id"]


def test_referral_reward_retries_after_inviter_gets_active_account(client, fresh_db):
    from app.db import (
        create_or_get_platform_user_by_phone,
        get_binding_intent,
        get_or_create_default_ai4all_account_for_user,
        get_or_create_personal_referral_code_for_user,
        get_wallet_summary,
        list_referral_relationships,
        set_account_onboarding_state,
    )
    from app.routers.web import _complete_binding_intent_from_wait_result

    fresh_db.rate_limit_daily = 0
    inviter = create_or_get_platform_user_by_phone(phone="13800000320")
    code = get_or_create_personal_referral_code_for_user(
        platform_user_id=inviter["id"],
    )

    with patch("app.routers.web._schedule_binding_wait"), patch(
        "app.openclaw_gateway.start_weixin_qr_login",
        return_value={
            "qrDataUrl": "data:image/png;base64,ZmFrZQ==",
            "sessionKey": "referral-retry-session",
            "message": "scan",
        },
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        invitee_res = client.post(
            "/web/register-and-binding-intent",
            json={
                "phone": "13800000321",
                "otp_token": _get_verified_token("13800000321"),
                "invite_code": code["code"],
            },
        )
    assert invitee_res.status_code == 200
    invitee_data = invitee_res.json()

    _complete_binding_intent_from_wait_result(
        get_binding_intent(binding_intent_id=invitee_data["binding_intent"]["id"]),
        {"connected": True, "accountId": "referral-retry-bot@im.bot"},
    )
    set_account_onboarding_state(
        account_id=invitee_data["account"]["id"],
        state="complete",
    )

    payload_base = {
        "channel": "openclaw-weixin",
        "channel_account_id": "referral-retry-bot@im.bot",
        "account_id": "referral-retry-bot@im.bot",
        "session_key": "referral-retry-session",
        "sender_id": "peer-referral-retry",
        "chat_id": "chat-referral-retry",
        "chat_type": "private",
        "message_type": "text",
    }
    for idx, text in enumerate(
        [
            "我想让你帮我整理最近的计划安排",
            "请帮我一起复盘一下今天的事情",
            "我还想聊聊下一步怎么提高效率",
        ],
        start=1,
    ):
        res = client.post(
            "/openclaw/turn",
            json={
                **payload_base,
                "message_id": f"referral-retry-msg-{idx}",
                "text": text,
            },
            headers=BRIDGE_HEADERS,
        )
        assert res.status_code == 200

    relationships = list_referral_relationships(
        invitee_platform_user_id=invitee_data["platform_user"]["id"],
    )
    assert relationships[0]["status"] == "qualified"
    assert relationships[0]["review_status"] == "passed"
    assert relationships[0]["reward_ledger_id"] is None

    inviter_account = get_or_create_default_ai4all_account_for_user(
        platform_user_id=inviter["id"],
    )["account"]
    wallet = get_wallet_summary(account_id=inviter_account["id"])
    relationships = list_referral_relationships(
        invitee_platform_user_id=invitee_data["platform_user"]["id"],
    )
    assert relationships[0]["status"] == "rewarded"
    assert relationships[0]["reward_ledger_id"]
    assert wallet["display"]["balance"] == "2000"


def test_referral_reward_delays_after_weekly_soft_limit(client, fresh_db):
    from app.db import (
        connect,
        get_binding_intent,
        list_referral_relationships,
        set_account_onboarding_state,
    )
    from app.routers.web import _complete_binding_intent_from_wait_result

    fresh_db.rate_limit_daily = 0
    inviter_headers, inviter_login = _get_login_data("13800000400", client)
    inviter_user = inviter_login["platform_user"]

    code_res = client.get("/web/me/referral-code", headers=inviter_headers)
    assert code_res.status_code == 200
    invite_code = code_res.json()["referral_code"]["code"]

    for idx in range(1, 6):
        phone = f"1380000040{idx}"
        res = client.post(
            "/web/register",
            json={
                "phone": phone,
                "display_name": f"invitee-{idx}",
                "otp_token": _get_verified_token(phone),
                "invite_code": invite_code,
            },
        )
        assert res.status_code == 200

    with patch("app.routers.web._schedule_binding_wait"), patch(
        "app.openclaw_gateway.start_weixin_qr_login",
        return_value={
            "qrDataUrl": "data:image/png;base64,ZmFrZQ==",
            "sessionKey": "referral-soft-limit-session",
            "message": "scan",
        },
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        invitee_res = client.post(
            "/web/register-and-binding-intent",
            json={
                "phone": "13800000406",
                "otp_token": _get_verified_token("13800000406"),
                "invite_code": invite_code,
            },
        )
    assert invitee_res.status_code == 200
    invitee_data = invitee_res.json()

    inviter_relationships = list_referral_relationships(
        inviter_platform_user_id=inviter_user["id"],
        limit=10,
    )
    assert len(inviter_relationships) == 6
    relationships = list_referral_relationships(
        invitee_platform_user_id=invitee_data["platform_user"]["id"],
    )
    relationship = relationships[0]
    assert relationship["metadata"]["soft_review_required"] is True
    assert relationship["metadata"]["inviter_recent_registration_count"] == 6

    _complete_binding_intent_from_wait_result(
        get_binding_intent(binding_intent_id=invitee_data["binding_intent"]["id"]),
        {"connected": True, "accountId": "referral-soft-limit-bot@im.bot"},
    )
    set_account_onboarding_state(
        account_id=invitee_data["account"]["id"],
        state="complete",
    )

    payload_base = {
        "channel": "openclaw-weixin",
        "channel_account_id": "referral-soft-limit-bot@im.bot",
        "account_id": "referral-soft-limit-bot@im.bot",
        "session_key": "referral-soft-limit-session",
        "sender_id": "peer-referral-soft-limit",
        "chat_id": "chat-referral-soft-limit",
        "chat_type": "private",
        "message_type": "text",
    }
    for idx, text in enumerate(
        [
            "最近我想认真规划一下自己的学习节奏",
            "我需要有人帮我拆解工作和生活优先级",
            "这周我想形成一个可以坚持的运动安排",
        ],
        start=1,
    ):
        res = client.post(
            "/openclaw/turn",
            json={
                **payload_base,
                "message_id": f"referral-soft-limit-msg-{idx}",
                "text": text,
            },
            headers=BRIDGE_HEADERS,
        )
        assert res.status_code == 200

    relationships = list_referral_relationships(
        invitee_platform_user_id=invitee_data["platform_user"]["id"],
    )
    relationship = relationships[0]
    assert relationship["status"] == "qualified"
    assert relationship["review_status"] == "pending"
    assert relationship["reward_ledger_id"] is None
    assert relationship["metadata"]["reward_pending_reason"] == "soft_review_delayed_release"
    assert relationship["metadata"]["reward_release_after"]

    wallet_before = client.get("/web/me/wallet", headers=inviter_headers).json()
    assert wallet_before["wallet"]["display"]["balance"] == "1000"

    with connect() as conn:
        row = conn.execute(
            "SELECT metadata_json FROM referral_relationships WHERE id = ?",
            (relationship["id"],),
        ).fetchone()
        metadata = json.loads(row["metadata_json"])
        metadata["reward_release_after"] = "2000-01-01 00:00:00"
        conn.execute(
            """
            UPDATE referral_relationships
            SET metadata_json = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (json.dumps(metadata, ensure_ascii=False), relationship["id"]),
        )

    release = client.post(
        "/admin/referrals/release-due-rewards",
        params={"limit": 10},
        headers={"Authorization": "Bearer test-admin"},
    )
    assert release.status_code == 200
    assert release.json()["released_count"] == 1

    wallet_after = client.get("/web/me/wallet", headers=inviter_headers).json()
    assert wallet_after["wallet"]["display"]["balance"] == "2000"
    reward_entries = [
        item for item in wallet_after["ledger"]
        if item["source_type"] == "referral_reward"
    ]
    assert len(reward_entries) == 1

    relationships = list_referral_relationships(
        invitee_platform_user_id=invitee_data["platform_user"]["id"],
    )
    assert relationships[0]["status"] == "rewarded"
    assert relationships[0]["review_status"] == "passed"
    assert relationships[0]["reward_ledger_id"] == reward_entries[0]["id"]


def test_referral_delayed_release_tolerates_missing_review_account(client, fresh_db):
    from app.db import (
        connect,
        create_or_get_platform_user_by_phone,
        get_or_create_personal_referral_code_for_user,
        list_referral_relationships,
        release_due_referral_rewards,
    )

    inviter_headers, inviter_login = _get_login_data("13800000420", client)
    inviter_user = inviter_login["platform_user"]
    code = client.get("/web/me/referral-code", headers=inviter_headers).json()["referral_code"]
    code_row = get_or_create_personal_referral_code_for_user(
        platform_user_id=inviter_user["id"],
    )
    assert code_row["code"] == code["code"]
    invitee = create_or_get_platform_user_by_phone(phone="13800000421")

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO referral_relationships(
                id, inviter_platform_user_id, invitee_platform_user_id,
                referral_code_id, status, meaningful_message_count,
                review_status, metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, 'qualified', 3, 'pending', ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (
                "refrel_missing_review_account",
                inviter_user["id"],
                invitee["id"],
                code_row["id"],
                json.dumps(
                    {
                        "soft_review_required": True,
                        "reward_release_after": "2000-01-01 00:00:00",
                        "candidate_message_ids": [101, 102, 103],
                    },
                    ensure_ascii=False,
                ),
            ),
        )

    released = release_due_referral_rewards(limit=10)
    assert len([item for item in released if item["source_type"] == "referral_reward"]) == 1
    relationships = list_referral_relationships(
        invitee_platform_user_id=invitee["id"],
    )
    assert relationships[0]["status"] == "rewarded"
    assert relationships[0]["review_status"] == "passed"


def test_referral_soft_limit_ignores_rejected_relationships(client, fresh_db):
    from app.db import connect, list_referral_relationships

    inviter_headers, inviter_login = _get_login_data("13800000430", client)
    inviter_user = inviter_login["platform_user"]
    invite_code = client.get(
        "/web/me/referral-code",
        headers=inviter_headers,
    ).json()["referral_code"]["code"]

    rejected_ids = []
    for idx in range(1, 6):
        phone = f"1380000043{idx}"
        res = client.post(
            "/web/register",
            json={
                "phone": phone,
                "display_name": f"rejected-invitee-{idx}",
                "otp_token": _get_verified_token(phone),
                "invite_code": invite_code,
            },
        )
        assert res.status_code == 200
        relationship = list_referral_relationships(
            invitee_platform_user_id=res.json()["platform_user"]["id"],
        )[0]
        rejected_ids.append(relationship["id"])

    with connect() as conn:
        for relationship_id in rejected_ids:
            conn.execute(
                """
                UPDATE referral_relationships
                SET status = 'rejected',
                    review_status = 'failed',
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ?
                """,
                (relationship_id,),
            )

    res = client.post(
        "/web/register",
        json={
            "phone": "13800000436",
            "display_name": "valid-invitee-after-rejections",
            "otp_token": _get_verified_token("13800000436"),
            "invite_code": invite_code,
        },
    )
    assert res.status_code == 200
    relationships = list_referral_relationships(
        inviter_platform_user_id=inviter_user["id"],
        limit=10,
    )
    latest = [
        item for item in relationships
        if item["invitee_platform_user_id"] == res.json()["platform_user"]["id"]
    ][0]
    assert latest["metadata"].get("soft_review_required") is not True


def test_referral_soft_review_failed_status_is_not_overwritten(client, fresh_db):
    from app.db import (
        connect,
        create_or_get_platform_user_by_phone,
        get_or_create_default_ai4all_account_for_user,
        get_or_create_personal_referral_code_for_user,
        get_or_create_session,
        insert_message,
        list_referral_relationships,
        process_referral_message_for_account,
    )

    inviter_headers, inviter_login = _get_login_data("13800000440", client)
    inviter_user = inviter_login["platform_user"]
    code = get_or_create_personal_referral_code_for_user(
        platform_user_id=inviter_user["id"],
    )
    invitee = create_or_get_platform_user_by_phone(phone="13800000441")
    invitee_account = get_or_create_default_ai4all_account_for_user(
        platform_user_id=invitee["id"],
        display_name=None,
        plan="free",
    )["account"]
    session = get_or_create_session(
        account_id=invitee_account["id"],
        channel="openclaw-weixin",
        sender_id="manual-soft-review-sender",
        sender_name=None,
        chat_id="manual-soft-review-chat",
        session_key="manual-soft-review-session",
    )

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO referral_relationships(
                id, inviter_platform_user_id, invitee_platform_user_id,
                referral_code_id, status, meaningful_message_count,
                review_status, metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, 'qualified', 3, 'failed', ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (
                "refrel_soft_review_failed",
                inviter_user["id"],
                invitee["id"],
                code["id"],
                json.dumps(
                    {
                        "soft_review_required": True,
                        "reward_release_after": "2000-01-01 00:00:00",
                        "candidate_message_ids": [201, 202, 203],
                        "bound_account_id": invitee_account["id"],
                    },
                    ensure_ascii=False,
                ),
            ),
        )

    message_db_id = insert_message(
        account_id=invitee_account["id"],
        session_id=session["session"]["id"],
        message_id="soft-review-failed-extra-message",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="我想继续补充一个真实的使用场景",
    )
    assert message_db_id is not None

    process_referral_message_for_account(
        account_id=invitee_account["id"],
        message_db_id=message_db_id,
    )
    relationships = list_referral_relationships(
        invitee_platform_user_id=invitee["id"],
    )
    assert relationships[0]["status"] == "qualified"
    assert relationships[0]["review_status"] == "failed"
    assert relationships[0]["reward_ledger_id"] is None


def test_binding_wait_completion_binds_channel_account_to_precreated_account(client):
    from app.db import get_binding_intent, list_channel_bindings_for_account
    from app.routers.web import _complete_binding_intent_from_wait_result

    session_headers, login_data = _get_login_data("13800000005", client)
    user = login_data["platform_user"]
    account = login_data["account"]

    with patch("app.routers.web._schedule_binding_wait"), patch(
        "app.openclaw_gateway.start_weixin_qr_login",
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
    from app.routers.web import _complete_binding_intent_from_wait_result

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


@pytest.mark.slow
def test_bound_channel_account_routes_turn_to_precreated_account(client):
    from app.db import get_binding_intent
    from app.routers.web import _complete_binding_intent_from_wait_result

    session_headers, login_data = _get_login_data("13800000006", client)
    account = login_data["account"]

    with patch("app.routers.web._schedule_binding_wait"), patch(
        "app.openclaw_gateway.start_weixin_qr_login",
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


@pytest.mark.slow
def test_bound_weixin_normalized_channel_account_routes_to_precreated_account(client):
    from app.db import get_binding_intent
    from app.routers.web import _complete_binding_intent_from_wait_result

    session_headers, login_data = _get_login_data("13800000008", client)
    account = login_data["account"]

    with patch("app.routers.web._schedule_binding_wait"), patch(
        "app.openclaw_gateway.start_weixin_qr_login",
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


@pytest.mark.slow
def test_bound_login_session_key_routes_turn_to_precreated_account(client):
    from app.db import get_binding_intent
    from app.routers.web import _complete_binding_intent_from_wait_result

    session_headers, login_data = _get_login_data("13800000007", client)
    account = login_data["account"]

    with patch("app.routers.web._schedule_binding_wait"), patch(
        "app.openclaw_gateway.start_weixin_qr_login",
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


def test_create_account_enforces_one_active_per_user_app(client):
    """A 收敛(§9.3 A4):同一 (user, app) 最多一个 active 账号,二次建号被拒。"""
    from app.db import create_platform_user_session
    from app.db.billing import (
        create_ai4all_account_for_user,
        get_first_active_account_for_user,
    )

    user = client.post(
        "/web/register",
        json={"phone": "13800009999",
              "otp_token": _get_verified_token("13800009999")},
    ).json()["platform_user"]
    create_platform_user_session(platform_user_id=user["id"], days=7)

    first = create_ai4all_account_for_user(
        platform_user_id=user["id"], display_name="第一个", require_display_name=False,
    )
    assert ACCOUNT_ID_RE.match(first["account"]["id"])

    with pytest.raises(ValueError, match="already has an active account"):
        create_ai4all_account_for_user(
            platform_user_id=user["id"], display_name="第二个", require_display_name=False,
        )

    # 解析器仍稳定返回那唯一账号。
    resolved = get_first_active_account_for_user(platform_user_id=user["id"])
    assert resolved["account"]["id"] == first["account"]["id"]


def test_owner_binding_partial_unique_index_enforces_and_allows_archived(fresh_db):
    """§9.3 A2:部分唯一索引在 DB 层挡住第二个 active (user,app);archived 不占名额。

    同时验证 SQLite 冲突消息含冲突列名——create_ai4all_account_for_user 的竞态兜底据此翻译。
    """
    from app.db._core import connect
    from app.db._backend import IntegrityError

    with connect() as conn:
        conn.execute("INSERT INTO platform_users(id, phone) VALUES ('u9','p9')")
        conn.execute("INSERT INTO accounts(id, app_id) VALUES ('acc-x','zhaoxi')")
        conn.execute("INSERT INTO accounts(id, app_id) VALUES ('acc-y','zhaoxi')")
        conn.execute(
            "INSERT INTO account_owner_bindings(platform_user_id, account_id, binding_method, status, app_id) "
            "VALUES ('u9','acc-x','m','active','zhaoxi')"
        )

    with pytest.raises(IntegrityError) as ei:
        with connect() as conn:
            conn.execute(
                "INSERT INTO account_owner_bindings(platform_user_id, account_id, binding_method, status, app_id) "
                "VALUES ('u9','acc-y','m','active','zhaoxi')"
            )
    msg = str(ei.value)
    # 兜底翻译条件(billing.create_ai4all_account_for_user):索引名(PG) 或 列名(SQLite) 命中。
    assert "ux_owner_binding_active_user_app" in msg or (
        "platform_user_id" in msg and "app_id" in msg
    )

    # archived 第二绑定允许(部分索引仅约束 active),收敛规范化(archive 非规范)不被索引阻断。
    with connect() as conn:
        conn.execute(
            "INSERT INTO account_owner_bindings(platform_user_id, account_id, binding_method, status, app_id) "
            "VALUES ('u9','acc-y','m','archived','zhaoxi')"
        )


def test_get_binding_intent_auto_expires_stale_qr(client):
    import app.db as db_module
    from app.db import get_binding_intent

    session_headers, login_data = _get_login_data("13800007777", client)

    with patch("app.routers.web._schedule_binding_wait"), patch(
        "app.openclaw_gateway.start_weixin_qr_login",
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
    res = client.get(f"/web/binding-intents/{intent['id']}", headers=session_headers)
    assert res.status_code == 200
    assert res.json()["binding_intent"]["status"] == "expired"


def test_channel_binding_deduplicates_by_channel_account_id(client):
    from app.db import (
        get_binding_intent,
        list_channel_bindings_for_account,
        upsert_channel_binding,
    )
    from app.routers.web import _complete_binding_intent_from_wait_result

    session_headers, login_data = _get_login_data("13800006666", client)
    account = login_data["account"]

    with patch("app.routers.web._schedule_binding_wait"), patch(
        "app.openclaw_gateway.start_weixin_qr_login",
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

    with patch("app.openclaw_gateway.logout_weixin_account") as mock_logout:
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
    # 拆包后 unbind_and_wipe_account 与 wipe_account_data 同在 app.db.lifecycle，
    # 内部按本模块名调用，故 patch 其所在模块（patch where it's used）。
    with patch("app.db.lifecycle.wipe_account_data", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            unbind_and_wipe_account(account_id=account_id)

    # 整体回滚：binding 仍在、账号仍未 deactivated。
    assert list_channel_bindings_for_account(account_id=account_id)
    assert get_account(account_id=account_id)["status"] != "deactivated"


def test_web_unbind_attempts_openclaw_weixin_logout(client):
    from app.db import get_binding_intent, list_channel_bindings_for_account
    from app.routers.web import _complete_binding_intent_from_wait_result

    session_headers, login_data = _get_login_data("13800000213", client)
    account = login_data["account"]
    intent = login_data["binding_intent"]
    _complete_binding_intent_from_wait_result(
        get_binding_intent(binding_intent_id=intent["id"]),
        {"connected": True, "accountId": "logout-bot@im.bot"},
    )
    assert list_channel_bindings_for_account(account_id=account["id"])

    with patch("app.openclaw_gateway.logout_weixin_account", return_value={"ok": True}) as mock_logout:
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
    from app.routers.web import _complete_binding_intent_from_wait_result

    session_headers, login_data = _get_login_data("13800000214", client)
    account = login_data["account"]
    intent = login_data["binding_intent"]
    _complete_binding_intent_from_wait_result(
        get_binding_intent(binding_intent_id=intent["id"]),
        {"connected": True, "accountId": "unsupported-bot@im.bot"},
    )

    with patch(
        "app.openclaw_gateway.logout_weixin_account",
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
