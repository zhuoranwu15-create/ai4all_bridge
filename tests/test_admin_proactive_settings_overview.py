"""主动消息设定在 admin proactive-overview 接口里的可见性（验证用后台）。"""

ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}


def _create_account(account_id: str) -> None:
    from app.db import get_or_create_session

    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"session-{account_id}",
    )


def test_overview_includes_default_effective_settings(client, fresh_db):
    _create_account("acc-ov1")
    res = client.get(
        "/admin/accounts/acc-ov1/proactive-overview",
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    settings = body["proactive_message_settings"]
    # 无设置行 → 有效视图返回全局默认
    assert settings["master_enabled"] is True
    assert settings["quiet_hours_is_user"] is False
    assert body["proactive_message_setting_events"] == []


def test_overview_reflects_update_and_audit(client, fresh_db):
    from app.proactive.settings import apply_proactive_message_settings_patch

    _create_account("acc-ov2")
    apply_proactive_message_settings_patch(
        account_id="acc-ov2",
        patch={
            "master_enabled": True,
            "category_updates": {"content_invitation": {"enabled": False}},
            "allowed_windows": [{"days": ["SAT", "SUN"], "start": "09:00", "end": "12:00"}],
            "frequency": {"content_invitation": {"max_per_week": 2}},
        },
        source="tool",
        reason="user_requested",
    )
    res = client.get(
        "/admin/accounts/acc-ov2/proactive-overview",
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    settings = body["proactive_message_settings"]
    assert settings["categories"]["content_invitation"]["enabled"] is False
    assert settings["allowed_windows"][0]["days"] == ["SAT", "SUN"]
    assert settings["frequency"]["content_invitation"]["max_per_week"] == 2
    events = body["proactive_message_setting_events"]
    assert events and events[0]["source"] == "tool"
    assert events[0]["reason"] == "user_requested"


def test_overview_requires_admin_auth(client, fresh_db):
    _create_account("acc-ov3")
    res = client.get("/admin/accounts/acc-ov3/proactive-overview")
    assert res.status_code == 401
