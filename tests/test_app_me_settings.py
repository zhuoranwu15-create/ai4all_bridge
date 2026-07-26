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


# --- ME-06/07 注销申请 -----------------------------------------------------


def test_deletion_request_is_idempotent_and_never_refreshes_cooling_period(client):
    headers, _ = _login(client, "13800139007")

    first = client.post(
        "/v1/me/account/deletion", headers=headers, json={"reason_code": "not_useful"}
    )
    assert first.status_code == 200, first.text
    created = first.json()
    assert created["cooling_days"] == me_settings.DELETION_COOLING_DAYS
    assert created["request"]["status"] == "pending"
    assert created["request"]["effective_at"].endswith("+08:00")
    # 运营字段不进客户端契约。
    assert "executed_by" not in created["request"]

    repeat = client.post(
        "/v1/me/account/deletion", headers=headers, json={"reason_code": "other"}
    ).json()
    assert repeat["request"]["request_id"] == created["request"]["request_id"]
    assert repeat["request"]["effective_at"] == created["request"]["effective_at"]
    assert repeat["request"]["reason_code"] == "not_useful"


def test_deletion_request_cancel_then_reapply_keeps_full_history(client):
    headers, _ = _login(client, "13800139008")
    first_id = client.post(
        "/v1/me/account/deletion", headers=headers, json={}
    ).json()["request"]["request_id"]

    cancelled = client.delete("/v1/me/account/deletion", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["request"]["status"] == "cancelled"
    assert client.get("/v1/me/account/deletion", headers=headers).json()["request"] is None

    second_id = client.post(
        "/v1/me/account/deletion", headers=headers, json={}
    ).json()["request"]["request_id"]
    assert second_id != first_id
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) c FROM account_deletion_requests"
            ).fetchone()["c"]
            == 2
        )


def test_cancel_without_open_request_is_not_found(client):
    headers, _ = _login(client, "13800139009")

    response = client.delete("/v1/me/account/deletion", headers=headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "deletion_request_not_found"


def test_deletion_request_rejects_free_text_reason(client):
    headers, _ = _login(client, "13800139010")

    response = client.post(
        "/v1/me/account/deletion",
        headers=headers,
        json={"reason_code": "我就是不想用了"},
    )

    assert response.status_code == 422


def test_deletion_request_is_isolated_across_users(client):
    """账号隔离：别人的注销申请不得出现在我的查询里，也不能被我撤销。"""
    headers_a, _ = _login(client, "13800139011")
    headers_b, _ = _login(client, "13800139012")
    client.post("/v1/me/account/deletion", headers=headers_a, json={})

    assert client.get("/v1/me/account/deletion", headers=headers_b).json()["request"] is None
    assert client.delete("/v1/me/account/deletion", headers=headers_b).status_code == 404
    assert (
        client.get("/v1/me/account/deletion", headers=headers_a).json()["request"][
            "status"
        ]
        == "pending"
    )


def test_mark_due_advances_status_without_touching_user_data(client):
    """冷静期到期只推状态到 due，服务端不自动清任何数据（清除由运营执行）。"""
    headers, login = _login(client, "13800139013")
    client.post("/v1/me/account/deletion", headers=headers, json={})
    user_id = login["platform_user"]["id"]

    now = beijing_now().replace(tzinfo=None, microsecond=0)
    from datetime import timedelta

    moved = me_settings.mark_due_deletion_requests(
        now=now + timedelta(days=me_settings.DELETION_COOLING_DAYS + 1)
    )

    assert moved == 1
    assert client.get("/v1/me/account/deletion", headers=headers).json()["request"][
        "status"
    ] == "due"
    assert db.get_platform_user(platform_user_id=user_id) is not None
    # due 之后仍可撤销：运营还没执行，用户仍有权反悔。
    assert client.delete("/v1/me/account/deletion", headers=headers).status_code == 200


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
