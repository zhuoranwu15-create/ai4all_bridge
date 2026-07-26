"""S6「我的」Tab 收尾：Profile（ME-01）、注销申请（ME-06/07）、通知偏好（ME-10）。"""
import json

import app.db as db
from app.platform.channels import CHANNEL_APP
from app.products.zhaoxi.application import AppInboxAdapter
from app.products.zhaoxi.domain.user_profile import USER_AVATAR_KEYS
from app.products.zhaoxi.infrastructure.persistence import me_settings
from app.products.zhaoxi.proactive.contract.common import _select_route
from app.products.zhaoxi.proactive.delivery.outbound import dispatch_proactive_text
from app.time_utils import beijing_naive_now, beijing_now
from tests.factories import make_resident_account


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


# --- ME-01 Profile --------------------------------------------------------


def test_profile_options_expose_controlled_avatars_and_limits(client):
    headers, _ = _login(client, "13800139000")

    response = client.get("/v1/me/profile-options", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert [item["key"] for item in data["avatars"]] == list(USER_AVATAR_KEYS)
    assert all(item["avatar_ref"] for item in data["avatars"])
    assert data["limits"]["nickname_chars"] >= data["limits"]["nickname_min_chars"] >= 1
    assert response.headers["Cache-Control"] == "no-store"


def test_profile_update_persists_and_me_reflects_it(client):
    headers, login = _login(client, "13800139001")
    avatar_key = next(iter(USER_AVATAR_KEYS))

    response = client.patch(
        "/v1/me/profile",
        headers=headers,
        json={"display_name": "小满", "avatar_key": avatar_key},
    )

    assert response.status_code == 200, response.text
    updated = response.json()["platform_user"]
    assert updated["display_name"] == "小满"
    assert updated["avatar_key"] == avatar_key
    assert updated["avatar_ref"].endswith(".png")
    # 手机号在任何 Profile 响应里都只出脱敏值。
    assert "13800139001" not in response.text
    assert updated["phone_masked"] == login["platform_user"]["phone_masked"]

    me = client.get("/v1/me", headers=headers).json()
    assert me["platform_user"]["display_name"] == "小满"
    assert me["platform_user"]["avatar_key"] == avatar_key


def test_profile_update_accepts_single_field_and_rejects_empty_payload(client):
    headers, _ = _login(client, "13800139002")
    avatar_key = next(iter(USER_AVATAR_KEYS))
    client.patch(
        "/v1/me/profile",
        headers=headers,
        json={"display_name": "阿吉", "avatar_key": avatar_key},
    )

    # 只传头像不应清空昵称：None 表示「本次不改」，不是「清空」。
    only_avatar = client.patch(
        "/v1/me/profile", headers=headers, json={"avatar_key": avatar_key}
    )
    assert only_avatar.status_code == 200
    assert only_avatar.json()["platform_user"]["display_name"] == "阿吉"

    empty = client.patch("/v1/me/profile", headers=headers, json={})
    assert empty.status_code == 422
    assert empty.json()["detail"] == "profile_update_empty"


def test_profile_update_rejects_bad_nickname_and_unknown_avatar_key(client):
    headers, _ = _login(client, "13800139003")

    too_long = client.patch(
        "/v1/me/profile", headers=headers, json={"display_name": "满" * 40}
    )
    assert too_long.status_code == 422

    blank = client.patch(
        "/v1/me/profile", headers=headers, json={"display_name": "   "}
    )
    assert blank.status_code == 422
    assert blank.json()["detail"] == "nickname_length_invalid"

    charset = client.patch(
        "/v1/me/profile", headers=headers, json={"display_name": "小​满"}
    )
    assert charset.status_code == 422
    assert charset.json()["detail"] == "nickname_charset_invalid"

    unknown_avatar = client.patch(
        "/v1/me/profile", headers=headers, json={"avatar_key": "resident_01"}
    )
    assert unknown_avatar.status_code == 422
    assert unknown_avatar.json()["detail"] == "avatar_key_invalid"

    # 未知 key 不得落库。
    assert client.get("/v1/me", headers=headers).json()["platform_user"][
        "avatar_key"
    ] is None


def test_nickname_hard_reject_and_sanitizer_outage_fail_closed(
    client, monkeypatch
):
    """昵称对外可见，走与自建角色名同一条 D-B 红线；清洗器不可用时 fail closed。"""
    headers, _ = _login(client, "13800139004")

    monkeypatch.setattr(
        "app.platform.moderation.text_sanitizer.generate_completion",
        lambda *_a, **_k: json.dumps(
            {"verdict": "reject", "categories": ["real_person_replication"]}
        ),
    )
    rejected = client.patch(
        "/v1/me/profile", headers=headers, json={"display_name": "某真人明星"}
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "content_rejected"

    def _boom(*_args, **_kwargs):
        raise TimeoutError("moderation upstream timeout")

    monkeypatch.setattr(
        "app.platform.moderation.text_sanitizer.generate_completion", _boom
    )
    unavailable = client.patch(
        "/v1/me/profile", headers=headers, json={"display_name": "随便"}
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"] == "content_review_unavailable"

    assert client.get("/v1/me", headers=headers).json()["platform_user"][
        "display_name"
    ] != "某真人明星"


def test_profile_update_is_scoped_to_the_calling_user(client):
    """账号隔离：改自己的 Profile 不得影响另一个真人。"""
    headers_a, _ = _login(client, "13800139005")
    headers_b, _ = _login(client, "13800139006")
    client.patch("/v1/me/profile", headers=headers_a, json={"display_name": "甲"})

    client.patch("/v1/me/profile", headers=headers_b, json={"display_name": "乙"})

    assert (
        client.get("/v1/me", headers=headers_a).json()["platform_user"]["display_name"]
        == "甲"
    )


# --- ME-06/07 注销：立即清除 -----------------------------------------------


def test_deletion_executes_immediately_and_revokes_session(client):
    headers, login = _login(client, "13800139007")
    user_id = login["platform_user"]["id"]

    response = client.post(
        "/v1/me/account/deletion",
        headers=headers,
        json={"confirm": True, "reason_code": "not_useful"},
    )

    assert response.status_code == 200, response.text
    request = response.json()["request"]
    assert request["status"] == "executed"
    assert request["reason_code"] == "not_useful"
    assert request["executed_at"].endswith("+08:00")
    # 运营字段不进客户端契约。
    assert "purge_stats_json" not in request
    # 注销后旧 token 立即失效，客户端必须回登录页。
    assert client.get("/v1/me", headers=headers).status_code == 401
    # 手机号不被永久占用：platform_users 行保留，可重新注册成全新用户。
    assert db.get_platform_user(platform_user_id=user_id) is not None


def test_deletion_purges_chat_records_and_memories(client, fresh_db):
    """核心口径（Q14）：注销后聊天原文、账号级记忆与世界级共享记忆都必须消失。"""
    fresh_db.companion_world_p1_enabled = True
    headers, account_id, user_id = _ready_user(client, "13800139008", "小满")
    scope = db.resolve_resident_memory_scope(runtime_account_id=account_id)
    session = db.get_or_create_session(
        account_id=account_id,
        channel=CHANNEL_APP,
        sender_id=user_id,
        sender_name=None,
        chat_id=None,
        session_key="__app_active__",
    )["session"]
    db.insert_message(
        account_id=account_id,
        session_id=int(session["id"]),
        message_id="msg-delete-me",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="我怕黑",
    )
    db.append_universe_fact(
        universe_id=scope["universe_id"],
        fact_type="user_preference",
        payload_json='{"memory_text":"用户怕黑","operation":"add"}',
        source_account_id=account_id,
        source_resident_id=scope["resident_id"],
        source_message_id="msg-delete-me",
        occurred_at="2026-07-26T09:00:00+08:00",
    )
    assert db.list_recent_messages_for_account(account_id=account_id, limit=10)
    assert db.read_universe_facts(universe_id=scope["universe_id"])

    client.post("/v1/me/account/deletion", headers=headers, json={"confirm": True})

    assert db.list_recent_messages_for_account(account_id=account_id, limit=10) == []
    assert db.read_universe_facts(universe_id=scope["universe_id"], status=None) == []
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) c FROM ai_conversations WHERE universe_id = ?",
                (scope["universe_id"],),
            ).fetchone()["c"]
            == 0
        )
        # 世界行本身受 UNIQUE(owner_platform_user_id) 约束保留，但回到引导前状态。
        assert (
            conn.execute(
                "SELECT onboarding_state FROM universes WHERE id = ?",
                (scope["universe_id"],),
            ).fetchone()["onboarding_state"]
            == "preparing"
        )
    # 居民全部遣散：重新登录拿到的是干净的新世界。
    assert db.list_residents(
        universe_id=scope["universe_id"], statuses=("active", "offline", "candidate")
    ) == []


def test_deletion_requires_explicit_confirmation(client):
    """不可撤销的破坏性操作不接受空 body：漏传 confirm 必须失败且不动数据。"""
    headers, login = _login(client, "13800139009")

    assert client.post("/v1/me/account/deletion", headers=headers, json={}).status_code == 422
    not_confirmed = client.post(
        "/v1/me/account/deletion", headers=headers, json={"confirm": False}
    )
    assert not_confirmed.status_code == 422
    assert not_confirmed.json()["detail"] == "deletion_not_confirmed"
    # 未确认时会话仍然有效。
    assert client.get("/v1/me", headers=headers).status_code == 200
    assert (
        me_settings.get_last_deletion_record(
            platform_user_id=login["platform_user"]["id"], app_id="zhaoxi"
        )
        is None
    )


def test_deletion_rejects_free_text_reason(client):
    headers, _ = _login(client, "13800139010")

    response = client.post(
        "/v1/me/account/deletion",
        headers=headers,
        json={"confirm": True, "reason_code": "我就是不想用了"},
    )

    assert response.status_code == 422
    # 校验失败不得留下任何清除痕迹。
    assert client.get("/v1/me", headers=headers).status_code == 200


def test_deletion_is_isolated_across_users(client, fresh_db):
    """账号隔离：注销自己不得动到另一个真人的数据与登录态。"""
    fresh_db.companion_world_p1_enabled = True
    headers_a, account_a, _ = _ready_user(client, "13800139011", "甲居民")
    headers_b, account_b, _ = _ready_user(client, "13800139012", "乙居民")
    scope_b = db.resolve_resident_memory_scope(runtime_account_id=account_b)
    session_b = db.get_or_create_session(
        account_id=account_b,
        channel=CHANNEL_APP,
        sender_id="sender-b",
        sender_name=None,
        chat_id=None,
        session_key="__app_active__",
    )["session"]
    db.insert_message(
        account_id=account_b,
        session_id=int(session_b["id"]),
        message_id="msg-keep-me",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="乙的消息",
    )
    db.append_universe_fact(
        universe_id=scope_b["universe_id"],
        fact_type="user_preference",
        payload_json='{"memory_text":"乙的记忆","operation":"add"}',
        source_account_id=account_b,
        source_resident_id=scope_b["resident_id"],
        source_message_id="msg-keep-me",
        occurred_at="2026-07-26T09:00:00+08:00",
    )

    client.post("/v1/me/account/deletion", headers=headers_a, json={"confirm": True})

    assert client.get("/v1/me", headers=headers_b).status_code == 200
    assert [
        row["content"]
        for row in db.list_recent_messages_for_account(account_id=account_b, limit=10)
    ] == ["乙的消息"]
    assert db.read_universe_facts(universe_id=scope_b["universe_id"])
    residents_b = db.list_residents(universe_id=scope_b["universe_id"], statuses=("active",))
    assert [row["runtime_account_id"] for row in residents_b] == [account_b]
    assert account_a != account_b


def test_repeated_deletion_after_reregistration_keeps_full_history(client):
    """注销后可用同一手机号重新注册；第二次注销另起一条流水，不覆盖第一条。"""
    phone = "13800139013"
    headers, _ = _login(client, phone)
    first = client.post(
        "/v1/me/account/deletion", headers=headers, json={"confirm": True}
    ).json()["request"]["request_id"]

    headers_again, _ = _login(client, phone)
    second = client.post(
        "/v1/me/account/deletion", headers=headers_again, json={"confirm": True}
    ).json()["request"]["request_id"]

    assert second != first
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) c FROM account_deletion_requests"
            ).fetchone()["c"]
            == 2
        )


# --- ME-10 通知偏好 --------------------------------------------------------


def _ready_user(client, phone: str, name: str):
    headers, login = _login(client, phone)
    user_id = login["platform_user"]["id"]
    account_id = make_resident_account(user_id, name)
    scope = db.resolve_resident_memory_scope(runtime_account_id=account_id)
    db.set_universe_onboarding_state(
        universe_id=scope["universe_id"], onboarding_state="confirmed"
    )
    return headers, account_id, user_id


def test_notification_preferences_default_and_upsert(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    fresh_db.companion_world_app_inbox_enabled = True
    headers, _ = _login(client, "13800139014")

    default = client.get("/v1/notifications/preferences", headers=headers)
    assert default.status_code == 200, default.text
    assert default.json()["data"]["quiet_level"] == "standard"
    assert default.json()["data"]["available_levels"] == list(me_settings.QUIET_LEVELS)

    updated = client.patch(
        "/v1/notifications/preferences", headers=headers, json={"quiet_level": "quiet"}
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["quiet_level"] == "quiet"
    # 幂等：重复设置同一值不报错。
    assert (
        client.patch(
            "/v1/notifications/preferences",
            headers=headers,
            json={"quiet_level": "quiet"},
        ).json()["data"]["quiet_level"]
        == "quiet"
    )
    assert client.get("/v1/notifications/preferences", headers=headers).json()["data"][
        "quiet_level"
    ] == "quiet"


def test_notification_preferences_reject_unknown_level(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    fresh_db.companion_world_app_inbox_enabled = True
    headers, _ = _login(client, "13800139015")

    response = client.patch(
        "/v1/notifications/preferences", headers=headers, json={"quiet_level": "off"}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "quiet_level_invalid"


def test_notification_preferences_are_isolated_across_users(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    fresh_db.companion_world_app_inbox_enabled = True
    headers_a, _ = _login(client, "13800139016")
    headers_b, _ = _login(client, "13800139017")

    client.patch(
        "/v1/notifications/preferences", headers=headers_a, json={"quiet_level": "quiet"}
    )

    assert client.get("/v1/notifications/preferences", headers=headers_b).json()["data"][
        "quiet_level"
    ] == "standard"


def test_quiet_mode_cancels_new_dispatches_but_keeps_existing_inbox(client, fresh_db):
    """安静模式只压制**将来**的投递；已在箱内的通知不回收。

    走真实出站路径 `dispatch_proactive_text`，而不是底层 `deliver()`——门控刻意放在
    建 outbound 行之前，用户偏好必须落 `cancelled` 而非 `failed`。
    """
    fresh_db.companion_world_p1_enabled = True
    fresh_db.companion_world_app_inbox_enabled = True
    # 本用例断言的是安静模式，不是频次策略；放开分类日上限以免撞上 daily_limit。
    fresh_db.companion_followup_daily_limit = 5
    headers, account_id, user_id = _ready_user(client, "13800139018", "小满")

    def _dispatch(key: str):
        return dispatch_proactive_text(
            account_id=account_id,
            channel=CHANNEL_APP,
            channel_account_id=None,
            to_user_id=user_id,
            session_key=None,
            source="commitment",
            text=f"通知 {key}",
            idempotency_key=f"resident-obligation:v1:commitment:{key}",
            now=beijing_naive_now(),
            product_category="companion_followup",
        )

    assert _dispatch("before")["status"] == "sent"
    assert client.get("/v1/notifications", headers=headers).json()["data"][
        "unread_count"
    ] == 1

    client.patch(
        "/v1/notifications/preferences", headers=headers, json={"quiet_level": "quiet"}
    )
    assert AppInboxAdapter().can_deliver(account_id) is False
    assert _select_route(account_id) is None
    suppressed = _dispatch("after")
    assert suppressed["status"] == "cancelled"
    assert suppressed["error"] == "app_inbox_quiet_hours_preference"

    listed = client.get("/v1/notifications", headers=headers).json()["data"]
    assert listed["unread_count"] == 1
    assert [item["body"]["text"] for item in listed["items"]] == ["通知 before"]

    # 关掉安静模式后恢复投递。
    client.patch(
        "/v1/notifications/preferences",
        headers=headers,
        json={"quiet_level": "standard"},
    )
    assert _dispatch("after")["status"] == "sent"
    assert client.get("/v1/notifications", headers=headers).json()["data"][
        "unread_count"
    ] == 2
