"""Web 注册入口的营销活码归因集成测试（campaign_codes_technical_design.md §3/§6）。"""
from unittest.mock import patch

from app.products.zhaoxi.infrastructure.persistence.campaign import create_campaign_code, get_campaign_code


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


def _login(client, phone: str, campaign_code=None):
    with patch("app.routers.web._schedule_binding_wait"), patch(
        "app.platform.gateways.openclaw.start_weixin_qr_login",
        return_value={"qrDataUrl": "data:image/png;base64,ZmFrZQ==", "sessionKey": f"login-{phone}"},
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        payload = {"phone": phone, "verified_token": _get_verified_token(phone)}
        if campaign_code is not None:
            payload["campaign_code"] = campaign_code
        return client.post("/web/login", json=payload)


def test_valid_campaign_code_creates_attribution(client, fresh_db):
    from app.products.zhaoxi.infrastructure.persistence.campaign import get_campaign_attribution

    create_campaign_code(
        code="VALIDC",
        campaign_key="a",
        mission_id="mission_001",
        soul_preset_key="xiaotaiyang",
        onboarding_script_variant="欢迎参加活动",
    )

    res = _login(client, "13900000001", campaign_code="VALIDC")

    assert res.status_code == 200
    account_id = res.json()["account"]["id"]
    attribution = get_campaign_attribution(account_id=account_id)
    assert attribution is not None
    assert attribution["campaign_code"] == "VALIDC"
    assert attribution["mission_id"] == "mission_001"
    assert attribution["soul_preset_key"] == "xiaotaiyang"
    assert get_campaign_code(code="VALIDC")["used_count"] == 1


def test_valid_campaign_code_immediately_applies_soul_preset(client, fresh_db):
    from app.agent_runtime.persistence import profile_storage

    create_campaign_code(code="SOULC", campaign_key="a", soul_preset_key="ju")

    res = _login(client, "13900000002", campaign_code="SOULC")

    assert res.status_code == 200
    account_id = res.json()["account"]["id"]
    soul = profile_storage.read_file(account_id, "SOUL.md")
    assert soul is not None


def test_expired_campaign_code_registration_still_succeeds_without_attribution(client, fresh_db):
    from app.products.zhaoxi.infrastructure.persistence.campaign import get_campaign_attribution

    create_campaign_code(code="EXPC", campaign_key="a", expires_at="2000-01-01 00:00:00")

    res = _login(client, "13900000003", campaign_code="EXPC")

    assert res.status_code == 200
    account_id = res.json()["account"]["id"]
    assert get_campaign_attribution(account_id=account_id) is None


def test_disabled_campaign_code_registration_still_succeeds_without_attribution(client, fresh_db):
    from app.products.zhaoxi.infrastructure.persistence.campaign import get_campaign_attribution

    create_campaign_code(code="DISC", campaign_key="a", status="disabled")

    res = _login(client, "13900000004", campaign_code="DISC")

    assert res.status_code == 200
    account_id = res.json()["account"]["id"]
    assert get_campaign_attribution(account_id=account_id) is None


def test_unknown_campaign_code_registration_still_succeeds_without_attribution(client, fresh_db):
    from app.products.zhaoxi.infrastructure.persistence.campaign import get_campaign_attribution

    res = _login(client, "13900000005", campaign_code="NOSUCH")

    assert res.status_code == 200
    account_id = res.json()["account"]["id"]
    assert get_campaign_attribution(account_id=account_id) is None


def test_no_campaign_code_registration_succeeds(client, fresh_db):
    from app.products.zhaoxi.infrastructure.persistence.campaign import get_campaign_attribution

    res = _login(client, "13900000006")

    assert res.status_code == 200
    account_id = res.json()["account"]["id"]
    assert get_campaign_attribution(account_id=account_id) is None


def test_web_config_exposes_campaign_code_param(client, fresh_db):
    res = client.get("/web/config")
    assert res.status_code == 200
    assert res.json()["registration"]["campaign_code_param"] == "campaign_code"
    assert res.json()["product"] == {
        "app_id": "zhaoxi",
        "default_language": "zh-CN",
    }


def test_register_and_binding_intent_applies_campaign_attribution(client, fresh_db):
    from app.products.zhaoxi.infrastructure.persistence.campaign import get_campaign_attribution

    create_campaign_code(code="RBIC", campaign_key="a", mission_id="mission_002")
    phone = "13900000007"

    with patch("app.routers.web._schedule_binding_wait"), patch(
        "app.platform.gateways.openclaw.start_weixin_qr_login",
        return_value={"qrDataUrl": "data:image/png;base64,ZmFrZQ==", "sessionKey": "rbi-key"},
    ), patch("app.main.settings.openclaw_login_auto_start", True):
        res = client.post(
            "/web/register-and-binding-intent",
            json={
                "phone": phone,
                "otp_token": _get_verified_token(phone),
                "campaign_code": "RBIC",
            },
        )

    assert res.status_code == 200
    account_id = res.json()["account"]["id"]
    attribution = get_campaign_attribution(account_id=account_id)
    assert attribution is not None
    assert attribution["mission_id"] == "mission_002"
