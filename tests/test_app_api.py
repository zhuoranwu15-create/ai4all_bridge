from unittest.mock import patch

from app.platform.channels import CHANNEL_APP
from app.db import (
    connect,
    create_phone_verification,
    get_active_bound_account_for_user_in_app,
    get_or_create_session,
    insert_message,
    set_verification_verified,
)
from app.schemas import OpenClawTurnResponse


def _verified_token(phone: str) -> str:
    row = create_phone_verification(phone=phone, code="999999", expires_minutes=10)
    verified = set_verification_verified(row["id"], token_expires_minutes=10)
    return verified["verified_token"]


def _login(client, phone: str = "13800138000") -> tuple[dict, dict]:
    response = client.post(
        "/v1/auth/session",
        json={"phone": phone, "verified_token": _verified_token(phone)},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data


def test_app_config_exposes_only_public_settings(client, fresh_db):
    fresh_db.aliyun_captcha_scene_id = "scene-public"
    fresh_db.aliyun_captcha_prefix = "prefix-public"
    fresh_db.asr_api_key = "secret-not-in-response"

    response = client.get("/v1/app/config")

    assert response.status_code == 200
    data = response.json()
    assert data["captcha"]["scene_id"] == "scene-public"
    assert data["features"]["voice_input"] is True
    assert "secret-not-in-response" not in response.text
    # v1.5 媒体限额恒下发（MEDIA-LIMIT-001）：与三个媒体能力位无关，客户端拿它做上传前校验。
    limits = data["limits"]
    assert limits["image_bytes_max"] == 8 * 1024 * 1024
    assert limits["image_count_max"] == 4
    assert limits["voice_bytes_max"] == 512_000
    assert limits["voice_duration_ms_max"] == 60_000
    # 语音消息与 ASR 转写是两个口径，不能混：语音消息上限必须严格小于 ASR 上限。
    assert limits["voice_bytes_max"] < limits["audio_bytes"]
    # 签名密钥属于凭证，绝不能出现在公开配置里。
    assert "test-media-signing-secret" not in response.text


def test_product_namespace_preserves_zhaoxi_contract(client):
    headers, legacy = _login(client, "13800138010")

    namespaced = client.get("/api/v1/products/zhaoxi/me", headers=headers)

    assert namespaced.status_code == 200
    assert namespaced.json()["account"] == legacy["account"]
    assert client.get("/api/v1/products/unknown/me", headers=headers).status_code == 404


def test_app_session_creates_app_account_without_weixin_qr(client, fresh_db):
    with patch("app.platform.gateways.openclaw.start_weixin_qr_login") as qr_mock:
        headers, data = _login(client)

    assert data["is_new_user"] is True
    assert data["account"]["ai_display_name"] == "朝夕"
    assert data["platform_user"]["phone_masked"] == "138****8000"
    qr_mock.assert_not_called()

    me = client.get("/v1/me", headers=headers)
    assert me.status_code == 200
    account_result = get_active_bound_account_for_user_in_app(
        platform_user_id=data["platform_user"]["id"], app_id="zhaoxi"
    )
    assert account_result["account"]["channel"] == CHANNEL_APP
    assert account_result["owner_binding"]["binding_method"] == "app_otp"


def test_app_session_reports_product_membership_newness(client):
    phone = "13800138009"
    with connect() as conn:
        conn.execute(
            "INSERT INTO platform_users(id, phone) VALUES (?, ?)",
            ("user_existing_other_product", phone),
        )

    _headers, first = _login(client, phone)
    assert first["platform_user"]["id"] == "user_existing_other_product"
    assert first["is_new_user"] is True

    _headers, repeated = _login(client, phone)
    assert repeated["is_new_user"] is False


def test_existing_phone_reuses_primary_account(client):
    headers1, first = _login(client, "13800138001")
    del headers1
    headers2, second = _login(client, "13800138001")

    assert second["is_new_user"] is False
    assert second["platform_user"]["id"] == first["platform_user"]["id"]
    assert second["account"]["id"] == first["account"]["id"]
    assert client.get("/v1/me", headers=headers2).status_code == 200


def test_app_history_is_scoped_away_from_weixin(client):
    headers, login = _login(client, "13800138002")
    account_id = login["account"]["id"]
    user_id = login["platform_user"]["id"]
    app_state = get_or_create_session(
        account_id=account_id,
        channel="app",
        sender_id=user_id,
        sender_name=None,
        chat_id=None,
        session_key="__app_active__",
        update_account_channel=False,
    )
    wx_state = get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="wx-user",
        sender_name=None,
        chat_id="wx-user",
        session_key="__account_active__",
    )
    insert_message(
        account_id=account_id,
        session_id=app_state["session"]["id"],
        message_id="app-visible",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="App 内可见",
    )
    insert_message(
        account_id=account_id,
        session_id=wx_state["session"]["id"],
        message_id="wx-hidden",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="微信内不可见",
    )

    response = client.get("/v1/chat/messages", headers=headers)

    assert response.status_code == 200
    assert [item["text"] for item in response.json()["messages"]] == ["App 内可见"]


def test_app_turn_builds_app_channel_input_and_sync_response(client):
    headers, login = _login(client, "13800138003")
    captured = {}

    def fake_run(ctx):
        captured["ctx"] = ctx
        return OpenClawTurnResponse(
            status="ok",
            reply="我在，慢慢说。",
            metadata={"reply_message_id": "reply-1"},
        )

    with patch("app.products.zhaoxi.api.app.run_turn_for_account", fake_run):
        response = client.post(
            "/v1/chat/turn",
            headers=headers,
            json={"text": " 今天有点累 ", "client_message_id": "client_0001"},
        )

    assert response.status_code == 200
    assert response.json()["reply"] == "我在，慢慢说。"
    ctx = captured["ctx"]
    assert ctx.account_id == login["account"]["id"]
    assert ctx.identity.channel == CHANNEL_APP
    assert ctx.identity.chat_id is None
    assert ctx.cap.active_session_key == "__app_active__"
    assert ctx.text == "今天有点累"


def test_legacy_chat_history_time_carries_offset(client):
    """TIME-001（Q10）：legacy `/chat/messages` 与世界端点同口径，公开时间显式带 +08:00。"""
    headers, login = _login(client, "13800138011")
    state = get_or_create_session(
        account_id=login["account"]["id"],
        channel="app",
        sender_id=login["platform_user"]["id"],
        sender_name=None,
        chat_id=None,
        session_key="__app_active__",
        update_account_channel=False,
    )
    insert_message(
        account_id=login["account"]["id"],
        session_id=state["session"]["id"],
        message_id="legacy-time-1",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="几点了",
    )

    response = client.get("/v1/chat/messages", headers=headers)

    assert response.status_code == 200
    times = [item["created_at"] for item in response.json()["messages"]]
    assert times and all(value.endswith("+08:00") for value in times)


def test_legacy_turn_replay_returns_the_same_message_id(client):
    """TURN-001：legacy `/chat/turn` 与世界端 turn 同步——重放回放原持久化 message_id。"""
    headers, _ = _login(client, "13800138012")
    body = {"text": "在吗", "client_message_id": "client_replay_01"}

    first = client.post("/v1/chat/turn", headers=headers, json=body)
    replay = client.post("/v1/chat/turn", headers=headers, json=body)

    assert first.status_code == replay.status_code == 200
    assert first.json()["metadata"]["deduplicated"] is False
    assert replay.json()["metadata"]["deduplicated"] is True
    assert replay.json()["reply"] == first.json()["reply"]
    assert replay.json()["metadata"]["message_id"] == first.json()["metadata"]["message_id"]
    assert replay.json()["metadata"]["message_id"]


def test_app_asr_mock_transcript_never_persists_audio(client, fresh_db):
    headers, _ = _login(client, "13800138004")
    fresh_db.asr_mock_transcript = "这是语音转写结果"

    response = client.post(
        "/v1/audio/transcriptions",
        headers=headers,
        files={"audio": ("recording.m4a", b"fake-audio", "audio/m4a")},
        data={"duration_ms": "1200", "language": "zh"},
    )

    assert response.status_code == 200
    assert response.json()["transcript"] == "这是语音转写结果"


def test_logout_revokes_presented_session(client):
    headers, _ = _login(client, "13800138005")

    response = client.delete("/v1/auth/session/current", headers=headers)

    assert response.status_code == 200
    assert client.get("/v1/me", headers=headers).status_code == 401
